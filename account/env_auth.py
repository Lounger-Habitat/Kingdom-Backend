"""Authentication helpers for trusted Env Server to Backend calls.

The regular game JWT identifies a player.  It is intentionally not sufficient
for endpoints that mutate shared world state.  Those endpoints require a
separately issued access token carrying the ``env_server`` scope.
"""

from rest_framework_simplejwt.exceptions import AuthenticationFailed

from .authentication import GameJWTAuthentication


ENV_SERVER_SCOPE = "env_server"


class EnvServerJWTAuthentication(GameJWTAuthentication):
    """Accept only a valid Game JWT explicitly issued for the Env Server."""

    def get_user(self, validated_token):
        if (
            validated_token.get("scope") != ENV_SERVER_SCOPE
            or validated_token.get("aud") != "kingdom-env"
        ):
            raise AuthenticationFailed("Env Server token scope is required")
        return super().get_user(validated_token)


def validate_player_access_token(raw_token):
    """Validate a player access token without trusting a client-decoded payload."""
    if not raw_token or not isinstance(raw_token, str):
        raise AuthenticationFailed("playerAccessToken is required")

    authenticator = GameJWTAuthentication()
    validated_token = authenticator.get_validated_token(raw_token)
    if validated_token.get("scope") == ENV_SERVER_SCOPE:
        raise AuthenticationFailed("service token cannot authenticate a player session")
    return authenticator.get_user(validated_token)
