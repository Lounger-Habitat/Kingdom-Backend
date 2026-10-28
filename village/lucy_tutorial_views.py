from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from account.env_auth import EnvServerJWTAuthentication
from player.lucy_tutorial import handle
from player.tutorials import TutorialError


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def tutorial_lucy(request):
    try:
        return Response(dict(success=True, resultCode="OK", data=handle(request.data)))
    except TutorialError as error:
        return Response(dict(success=False, resultCode=error.code, message=error.message, data={}), status=400)
