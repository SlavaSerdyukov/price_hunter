from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from pricehunter.core.container import Container
from pricehunter.core.security import token_digest
from pricehunter.db.models import User

bearer = HTTPBearer(auto_error=False)


def get_container(request: Request) -> Container:
    return cast(Container, request.app.state.container)


ContainerDependency = Annotated[Container, Depends(get_container)]


async def current_user(
    container: ContainerDependency,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> User:
    if credentials is None or not 20 <= len(credentials.credentials) <= 200:
        raise HTTPException(
            status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Bearer"}
        )
    user = await container.users.authenticate(token_digest(credentials.credentials))
    if user is None:
        raise HTTPException(
            status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Bearer"}
        )
    return user


UserDependency = Annotated[User, Depends(current_user)]
