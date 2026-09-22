from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from account.env_auth import EnvServerJWTAuthentication
from . import forestry


@api_view(["POST"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def forestry_action(request):
    result = forestry.execute(request.data)
    return Response(result, status=409 if result["result_code"] == "REQUEST_CONFLICT" else 200)


@api_view(["GET"])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def forestry_bootstrap(request):
    return Response(forestry.bootstrap())
