from datetime import UTC, datetime

from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    AnswerPreCheckoutQuery,
    CreateInvoiceLink,
    EditMessageReplyMarkup,
    EditUserStarSubscription,
    GetMe,
    GetMyStarBalance,
    GetStarTransactions,
    RefundStarPayment,
    SendMessage,
    SendPhoto,
)
from aiogram.types import Chat, Message, StarAmount, StarTransactions, User


class FakeTelegramSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.calls = []
        self.transactions = []
        self.failures = {}

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109
        self.calls.append(method)
        failure = self.failures.pop(type(method), None)
        if failure:
            raise failure
        if isinstance(method, CreateInvoiceLink):
            return "https://t.me/$test_" + method.payload[4:]
        if isinstance(
            method, (AnswerPreCheckoutQuery, RefundStarPayment, EditUserStarSubscription)
        ):
            return True
        if isinstance(method, GetMyStarBalance):
            return StarAmount(amount=123)
        if isinstance(method, GetStarTransactions):
            start, limit = method.offset or 0, method.limit or 100
            return StarTransactions(transactions=self.transactions[start : start + limit])
        if isinstance(method, GetMe):
            return User(id=123456789, is_bot=True, first_name="PriceHunter", username="testbot")
        if isinstance(method, (SendMessage, SendPhoto)):
            message = Message(
                message_id=len(self.sent) + 1,
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text if isinstance(method, SendMessage) else method.caption,
                reply_markup=method.reply_markup,
            )
            self.sent.append(message)
            return message
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, EditMessageReplyMarkup):
            message = next(m for m in self.sent if m.message_id == method.message_id)
            updated = message.model_copy(update={"reply_markup": method.reply_markup})
            self.sent[self.sent.index(message)] = updated
            return updated
        raise AssertionError(f"Unexpected Telegram method {type(method).__name__}")

    async def stream_content(self, *args, **kwargs):
        yield b""
