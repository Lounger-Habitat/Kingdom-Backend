import re
from rest_framework import serializers
from .models import GameAccount


class RegisterSerializer(serializers.Serializer):
    username = serializers.CharField(min_length=3, max_length=20)
    password = serializers.CharField(min_length=6, max_length=50)

    def validate_username(self, value):
        if not re.match(r"^[a-zA-Z0-9_]+$", value):
            raise serializers.ValidationError("用户名只能包含字母、数字和下划线")
        if GameAccount.objects.filter(username=value).exists():
            raise serializers.ValidationError("用户名已存在")
        return value


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField()


class ConvertGuestSerializer(serializers.Serializer):
    new_username = serializers.CharField(min_length=3, max_length=20)
    new_password = serializers.CharField(min_length=6, max_length=50)

    def validate_new_username(self, value):
        if not re.match(r"^[a-zA-Z0-9_]+$", value):
            raise serializers.ValidationError("用户名只能包含字母、数字和下划线")
        if GameAccount.objects.filter(username=value).exists():
            raise serializers.ValidationError("用户名已存在")
        return value
