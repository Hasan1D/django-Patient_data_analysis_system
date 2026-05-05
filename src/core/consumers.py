from __future__ import annotations

from datetime import date
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken

from core.models import User
from core.services.map_cases import (
    MAP_ACTIVE_ALL_GROUP,
    disease_group_name,
    disease_type_group_name,
    normalize_disease_code,
    normalize_disease_type,
)


class ActiveCasesMapConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        query_params = parse_qs(self.scope["query_string"].decode())
        token = self._first_query_value(query_params, "token")
        user = await self._get_user_for_token(token)
        if user is None:
            await self.close(code=4401)
            return

        self.disease_code = normalize_disease_code(
            self._first_query_value(query_params, "disease_code")
        )
        self.disease_type = normalize_disease_type(
            self._first_query_value(query_params, "disease_type")
        )
        try:
            self.start_date = self._parse_date_filter(
                self._first_query_value(query_params, "start_date")
                or self._first_query_value(query_params, "date_from")
            )
            self.end_date = self._parse_date_filter(
                self._first_query_value(query_params, "end_date")
                or self._first_query_value(query_params, "date_to")
            )
        except ValueError:
            await self.close(code=4400)
            return

        if self.disease_code:
            self.group_name = disease_group_name(self.disease_code)
        elif self.disease_type:
            self.group_name = disease_type_group_name(self.disease_type)
        else:
            self.group_name = MAP_ACTIVE_ALL_GROUP

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self.send_json(
            {
                "type": "connection.accepted",
                "filters": {
                    "disease_code": self.disease_code,
                    "disease_type": self.disease_type,
                    "start_date": self.start_date.isoformat() if self.start_date else None,
                    "end_date": self.end_date.isoformat() if self.end_date else None,
                },
            }
        )

    async def disconnect(self, close_code):
        group_name = getattr(self, "group_name", None)
        if group_name:
            await self.channel_layer.group_discard(group_name, self.channel_name)

    async def map_case_event(self, event):
        payload = event.get("payload", {})
        case = payload.get("case", {})
        if not self._case_matches_filters(case):
            return
        await self.send_json(payload)

    @staticmethod
    def _first_query_value(query_params, key):
        values = query_params.get(key)
        if not values:
            return None
        return values[0]

    @staticmethod
    def _parse_date_filter(value):
        if not value:
            return None
        return date.fromisoformat(value)

    def _case_matches_filters(self, case):
        if self.disease_code and case.get("disease_code") != self.disease_code:
            return False
        if self.disease_type and case.get("disease_type") != self.disease_type:
            return False

        diagnosis_date = case.get("diagnosis_date")
        if diagnosis_date:
            diagnosis_date = date.fromisoformat(diagnosis_date)
            if self.start_date and diagnosis_date < self.start_date:
                return False
            if self.end_date and diagnosis_date > self.end_date:
                return False
        return True

    @database_sync_to_async
    def _get_user_for_token(self, token):
        if not token:
            return None
        try:
            validated_token = AccessToken(token)
            user_id = validated_token["user_id"]
        except (KeyError, TokenError):
            return None

        UserModel = get_user_model()
        try:
            user = UserModel.objects.get(id=user_id, is_active=True)
        except UserModel.DoesNotExist:
            return None

        if user.role not in (User.ROLE_DOCTOR, User.ROLE_ADMIN):
            return None
        return user
