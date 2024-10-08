from fastapi import Depends

from repository.user import UserRepo

from handlers.security import TokenHandler
from handlers.security import UserSession, CryptoUtils, PasswordHashing

from models.oauth import OauthRequest, AuthURL, OauthTokenRequest, OauthTokenParam
from models.user import CreateUser, PublicUser, QueryUser
from models.token import CreateSession, SessionStatus, Token, SetCookie

from config import settings

from urllib.parse import urlencode

from json import dumps, loads

import requests

class OauthHandler:
    def __init__(
        self, 
        user_repo: UserRepo = Depends(UserRepo),
        token_handler: TokenHandler = Depends(TokenHandler),
        user_session: UserSession = Depends(UserSession),
    ) -> None:
        self._repo = user_repo
        self._token_handler = token_handler
        self._user_session = user_session

    def get_oauth(self, oauth_req: OauthRequest) -> AuthURL:
        if oauth_req.provider not in settings.PROVIDER:
            raise Exception("Invalid provider")
        oauth_config = settings.OAUTH.get(oauth_req.provider)
        auth_url = oauth_config["auth_url"]
        client_id = oauth_config["client_id"]
        scope = " ".join(oauth_config["scope"])
        redirect_uri = settings.REDIRECT_URI
        cv, cc = CryptoUtils.gen_pkce()
        state = CryptoUtils.gen_secret().hex()
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state
        }
        if oauth_req.provider != "github":
            params.update({
                "response_type": "code",
                "code_challenge": cc,
                "code_challenge_method": "S256"
            })
        else:
            params.update({
                "prompt": "select_account"
            })
        query_string = urlencode(params)
        authorization_url = f"{auth_url}?{query_string}"
        self._user_session.repo.set_value(state, dumps({
            "cv": cv if oauth_req.provider != "github" else None,
            "provider": oauth_req.provider,
            "client_url": oauth_req.client_url
        }), exp=300)
        return AuthURL(url=authorization_url)
    
    def exchange_key(self, token_req: OauthTokenRequest) -> SetCookie:
        if token_req.error:
            raise Exception(token_req.error_description or "Error with Oauth2")
        oauth: dict = loads(self._user_session.repo.get_value(token_req.state))
        if not oauth:
            raise Exception("Authorization code expired")
        provider = oauth.get("provider")
        oauth_config = settings.OAUTH.get(provider)     
        token_param = OauthTokenParam(
            client_id=oauth_config.get("client_id"),
            client_secret=oauth_config.get("client_secret"),
            code=token_req.code,
            redirect_uri=settings.REDIRECT_URI,
            code_verifier=oauth.get("cv"),
            userinfo_url=oauth_config.get("userinfo_url"),
            token_url=oauth_config.get("token_url")
        )
        user_info = self.get_user_info(token_param)
        match provider:
            case "google":
                email = user_info.get("email")
            case "github":
                email = user_info[0].get("email")
            case _:
                email = None
        user = self._repo.get(QueryUser(email=email))
        if not user:
            password = CryptoUtils.gen_secret().hex()
            password_hash = PasswordHashing().hash(password)
            user = self._repo.create(CreateUser(email=email, password=password_hash))
        secret = self._user_session.repo.get_value(str(user.id))
        if secret:
            raise Exception("You already logged in")
        secret = CryptoUtils.gen_secret().hex() 
        payload: dict = {
            "sub": {"user_id": str(user.id)}
        }
        token = self._token_handler.gen_token(payload, secret)
        self._user_session.repo.set_value(str(user.id), secret, settings.SESSION_EXPIRE)
        session_info = CreateSession(
            ip=self._user_session.ip,
            location="Unknown",
            user_agent=self._user_session.ua,
            user_id=user.id,
            status=SessionStatus.ACTIVE,
            token=token
        )
        sessionDB = self._user_session.repo.create_session(session_info) 
        return SetCookie(access_token=token, session_id=str(sessionDB.id))

    def get_user_info(self, token_param: OauthTokenParam) -> dict | list:
        data = token_param.model_dump(exclude={"userinfo_url", "headers", "token_url"}, exclude_none=True)
        response = requests.post(token_param.token_url, data=data, headers=token_param.headers)
        response = response.json() 
        if not response or "access_token" not in response:
            raise Exception(response)
        access_token = response.get("access_token")
        user_info = requests.get(token_param.userinfo_url, headers={
            "Authorization": f"Bearer {access_token}"
        })
        return user_info.json()


        
        

    