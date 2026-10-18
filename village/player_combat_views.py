from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from account.env_auth import EnvServerJWTAuthentication


@api_view(['POST'])
@authentication_classes([EnvServerJWTAuthentication])
@permission_classes([IsAuthenticated])
def player_combat(request):
    from .player_hunting import execute_combat
    result = execute_combat(request.data)
    return Response(result, status=409 if result['result_code'] == 'REQUEST_CONFLICT' else 200)
