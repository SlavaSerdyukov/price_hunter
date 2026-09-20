import asyncio
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PreCheckoutQuery,
)

from pricehunter.bot.keyboards import Action, button
from pricehunter.bot.payment_updates import stars_payment
from pricehunter.core.container import Container
from pricehunter.db.models import User
from pricehunter.domain.errors import PriceHunterError
from pricehunter.domain.subscriptions import Feature, Plan
from pricehunter.localization.languages import DEFAULT_LANGUAGE, normalize_language
from pricehunter.localization.messages import tr


def subscription_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[button(language, "my_subscription", "subscription")]]
    )


async def show_plans(message: Message, container: Container, user: User) -> None:
    language = user.language_code
    current = await container.entitlements.for_user(user.id)
    lines = [tr(language, "plans_heading", plan=current.plan.title())]
    rows = []
    for plan in Plan:
        limits = container.policy.for_plan(plan)
        price = "0" if plan == Plan.FREE else str(container.catalog.for_plan(plan).stars)
        features = ", ".join(
            tr(language, "feature_" + f.value) for f in Feature if limits.allows(f)
        )
        lines.append(
            tr(
                language,
                "plan_details",
                plan=plan.title(),
                price=price,
                trackers=limits.max_trackers,
                hours=f"{limits.check_interval_seconds / 3600:g}",
                search=limits.search_limit,
                results=limits.search_result_limit,
                history=limits.history_days,
                features=features,
            )
        )
        if container.settings.stars_billing_enabled and container.policy.rank(
            plan
        ) > container.policy.rank(current.plan):
            rows.append([button(language, "upgrade_" + plan, "buy", plan)])
    lines.append(
        tr(
            language,
            "billing_terms" if container.settings.stars_billing_enabled else "billing_disabled",
        )
    )
    rows.append([button(language, "my_subscription", "subscription")])
    await message.answer(
        "\n\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


async def show_subscription(message: Message, container: Container, user: User) -> None:
    language = user.language_code
    status = await container.subscriptions.status(user.id)
    renewal = (
        "renewal_unknown"
        if status.auto_renew is None
        else "renewal_last_enabled"
        if status.auto_renew
        else "renewal_cancelled"
    )
    rows = [[button(language, "plans", "plans")]]
    for subscription in await container.subscriptions.renewable(user.id):
        rows.append(
            [
                InlineKeyboardButton(
                    text=tr(language, "cancel_plan_renewal", plan=subscription.plan.title()),
                    callback_data=Action(action="cancelrenew", value=subscription.id.hex).pack(),
                )
            ]
        )
    await message.answer(
        tr(
            language,
            "subscription_details",
            plan=status.plan.title(),
            status=tr(language, "subscription_" + status.status),
            until=status.valid_until.astimezone(ZoneInfo(user.timezone)).strftime(
                "%Y-%m-%d %H:%M %Z"
            )
            if status.valid_until
            else "—",
            renewal=tr(language, renewal),
            count=status.tracker_count,
            active=status.scheduled_tracker_count,
            limit=status.tracker_limit,
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


def build_billing_router() -> Router:
    router = Router(name="billing")

    @router.message(Command("plans"))
    async def plans(message: Message, container: Container, user: User) -> None:
        await show_plans(message, container, user)

    @router.message(Command("subscription"))
    async def subscription(message: Message, container: Container, user: User) -> None:
        await show_subscription(message, container, user)

    @router.message(Command("paysupport"))
    async def support(message: Message, container: Container, language: str) -> None:
        await message.answer(tr(language, "support", contact=container.settings.support_contact))

    @router.callback_query(
        Action.filter(
            F.action.in_({"plans", "subscription", "buy", "cancelrenew", "confirmcancel"})
        )
    )
    async def billing_callback(
        query: CallbackQuery, callback_data: Action, container: Container, user: User, language: str
    ) -> None:
        if not isinstance(query.message, Message):
            await query.answer()
            return
        await query.answer()
        action, value, message = callback_data.action, callback_data.value, query.message
        if action == "plans":
            await show_plans(message, container, user)
        elif action == "subscription":
            await show_subscription(message, container, user)
        elif action == "buy":
            plan = Plan(value)
            checkout = await container.billing.create_checkout(
                user.id,
                plan,
                title=tr(language, "invoice_title", plan=plan.title()),
                description=tr(language, "invoice_description", plan=plan.title()),
            )
            await message.answer(
                tr(language, "checkout_ready", plan=plan.title(), price=checkout.product.stars),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(text=tr(language, "pay_stars"), url=checkout.url),
                        ]
                    ]
                ),
            )
        elif action == "cancelrenew":
            await message.answer(
                tr(language, "cancel_renewal_confirm"),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            button(language, "confirm_cancel_renewal", "confirmcancel", value),
                            button(language, "back", "subscription"),
                        ]
                    ]
                ),
            )
        elif action == "confirmcancel":
            await container.billing.cancel_renewal(user.id, UUID(value))
            await message.answer(tr(language, "renewal_cancelled_confirmation"))
            await show_subscription(message, container, user)

    @router.pre_checkout_query()
    async def pre_checkout(query: PreCheckoutQuery, container: Container) -> None:
        error = None
        language = normalize_language(query.from_user.language_code or DEFAULT_LANGUAGE)
        try:
            async with asyncio.timeout(5):
                language = await container.users.language(query.from_user.id, fallback=language)
                await container.billing.precheckout(
                    query.from_user.id,
                    query.invoice_payload,
                    query.currency,
                    query.total_amount,
                    query.id,
                )
        except Exception as exc:
            error = tr(
                language,
                exc.code if isinstance(exc, PriceHunterError) else "payment_rejected",
            )
            structlog.get_logger().warning("precheckout_rejected", error_type=type(exc).__name__)
        await query.answer(ok=error is None, error_message=error, request_timeout=3)

    @router.message(F.successful_payment)
    async def successful(message: Message, container: Container) -> None:
        payment = message.successful_payment
        assert payment is not None and message.from_user is not None
        incoming = stars_payment(message)
        identity = await container.billing_intake.receive(incoming)
        result = await container.billing_intake.process(identity)
        language = await container.users.language(
            message.from_user.id, fallback=message.from_user.language_code
        )
        if result and result.applied:
            await message.answer(
                tr(
                    language,
                    "payment_successful",
                    plan=result.plan.title(),
                    until=result.valid_until.strftime("%Y-%m-%d %H:%M UTC"),
                ),
                reply_markup=subscription_keyboard(language),
            )
        elif result is None:
            status = await container.billing_intake.status(identity)
            if status != "processed":
                await message.answer(
                    tr(
                        language,
                        "payment_rejected" if status == "rejected" else "payment_processing",
                    ),
                    reply_markup=subscription_keyboard(language),
                )

    @router.message(F.refunded_payment)
    async def refunded(message: Message, container: Container) -> None:
        payment = message.refunded_payment
        assert payment is not None
        incoming = stars_payment(message)
        identity = await container.billing_intake.receive(incoming, refund=True)
        await container.billing_intake.process(identity)

    return router
