import logging
from typing import Dict, Any, Optional
import json
import urllib.parse
import threading
import requests

import attr

from synapse.module_api import ModuleApi
from synapse.module_api.errors import ConfigError
from synapse.types import JsonDict, UserID
from synapse.spam_checker_api import RegistrationBehaviour

logger = logging.getLogger(__name__)

@attr.s(auto_attribs=True, frozen=True)
class RegistrationMonitorConfig:
    notification_room: str
    admin_token: str  # Required admin token for API access
    suspend_users: bool = True
    force_join_room: bool = True
    admin_user: Optional[str] = None
    server_name: Optional[str] = None
    reason: str = "Account suspended pending review"
    homeserver_url: str = "http://localhost:8008"  # Default to localhost

class RegistrationMonitor:
    def __init__(self, config: JsonDict, api: ModuleApi):
        # Validate and process config
        if not config.get("notification_room"):
            raise ConfigError("Missing required config field 'notification_room'")

        if not config.get("admin_token"):
            raise ConfigError("Missing required config field 'admin_token'")

        self.config = RegistrationMonitorConfig(**config)
        self.api = api

        # Register our spam checker callback
        self.api.register_spam_checker_callbacks(
            check_registration_for_spam=self.check_registration_for_spam
        )

        # Register callback for when a user is created
        self.api.register_account_validity_callbacks(
            on_user_registration=self.user_created_callback
        )

        logger.info("RegistrationMonitor initialized with config: %s", self.config)

    async def check_registration_for_spam(
        self, email_threepid: Optional[Dict[str, str]], username: Optional[str],
        source_ip: Optional[str], auth_provider_id: Optional[str]
    ) -> RegistrationBehaviour:
        """
        Called when a user attempts to register.

        Args:
            email_threepid: Dict containing "address" and "medium", or None if no email provided
            username: The desired username, or None if not specified yet
            source_ip: The IP address making the request, or None if unknown
            auth_provider_id: The SSO provider ID, or None if not using SSO

        Returns:
            RegistrationBehaviour indicating what action to take
        """
        if not username:
            return RegistrationBehaviour.ALLOW

        # Prepare notification message
        email = email_threepid.get("address", "No email provided") if email_threepid else "No email provided"
        ip = source_ip or "Unknown IP"
        auth = auth_provider_id or "password"

        message = f"📝 New registration detected:\n" \
                 f"- Username: @{username}:{self.api.server_name}\n" \
                 f"- Email: {email}\n" \
                 f"- IP Address: {ip}\n" \
                 f"- Auth Method: {auth}"

        if self.config.suspend_users:
            message += "\n✋ User will be automatically suspended after registration."

        # Send notification to the specified room
        try:
            await self.api.create_and_send_event_into_room({
                "room_id": self.config.notification_room,
                "type": "m.room.message",
                "sender": self.config.admin_user or f"@admin:{self.config.server_name or self.api.server_name}",
                "content": {
                    "msgtype": "m.text",
                    "body": message
                }
            })
            logger.info("Sent registration notification for user %s", username)
        except Exception as e:
            logger.error("Failed to send registration notification: %s", e)

        # Let registration proceed - we'll suspend after creation in the callback
        return RegistrationBehaviour.ALLOW

    async def user_created_callback(self, user_id: str) -> None:
        """Called when a new user has been created."""
        actions_performed = []

        # Force join the user to the notification room if configured
        if self.config.force_join_room:
            success = await self._force_join_room(user_id, self.config.notification_room)
            if success:
                actions_performed.append("joined to notification room")

        # Suspend the user if configured
        if self.config.suspend_users:
            success = await self._suspend_user(user_id)
            if success:
                actions_performed.append("suspended")

        # Send confirmation message
        if actions_performed:
            actions_text = " and ".join(actions_performed)
            message = f"✅ User {user_id} has been {actions_text}."

            try:
                await self.api.create_and_send_event_into_room({
                    "room_id": self.config.notification_room,
                    "type": "m.room.message",
                    "sender": self.config.admin_user or f"@admin:{self.config.server_name or self.api.server_name}",
                    "content": {
                        "msgtype": "m.text",
                        "body": message
                    }
                })
            except Exception as e:
                logger.error("Failed to send confirmation message: %s", e)

    def _suspend_user_thread(self, user_id: str, result_list):
        """
        Synchronous function to suspend a user using the admin API.
        This runs in a separate thread.
        """
        try:
            # Encode the user_id for the URL
            encoded_user_id = urllib.parse.quote(user_id)

            # Use the suspension endpoint
            suspend_url = f"{self.config.homeserver_url}/_synapse/admin/v1/suspend/{encoded_user_id}"

            headers = {
                "Authorization": f"Bearer {self.config.admin_token}",
                "Content-Type": "application/json"
            }

            suspend_data = {
                "suspend": True
            }

            response = requests.put(
                suspend_url,
                headers=headers,
                json=suspend_data,
                timeout=30.0
            )

            if response.status_code == 200:
                logger.info("Successfully suspended user %s", user_id)
                result_list.append(True)
            else:
                logger.error(
                    "Failed to suspend user %s: HTTP %d: %s",
                    user_id, response.status_code, response.text
                )
                result_list.append(False)

        except Exception as e:
            logger.error("Error suspending user %s: %s", user_id, e)
            result_list.append(False)

    def _force_join_room_thread(self, user_id: str, room_id: str, result_list):
        """
        Synchronous function to force a user to join a room using the admin API.
        This runs in a separate thread.
        """
        try:
            # URL encode room_id since it contains special characters
            encoded_room_id = urllib.parse.quote(room_id)
            url = f"{self.config.homeserver_url}/_synapse/admin/v1/join/{encoded_room_id}"

            headers = {
                "Authorization": f"Bearer {self.config.admin_token}",
                "Content-Type": "application/json"
            }

            data = {"user_id": user_id}

            response = requests.post(
                url,
                headers=headers,
                json=data,
                timeout=30.0
            )

            if response.status_code == 200:
                logger.info("Successfully joined user %s to room %s", user_id, room_id)
                result_list.append(True)
            else:
                logger.error(
                    "Failed to join user %s to room %s: HTTP %d: %s",
                    user_id, room_id, response.status_code, response.text
                )
                result_list.append(False)

        except Exception as e:
            logger.error("Error joining user %s to room %s: %s", user_id, room_id, e)
            result_list.append(False)

    async def _suspend_user(self, user_id: str) -> bool:
        """
        Async wrapper to run the synchronous suspension function in a thread.
        """
        result_list = []
        thread = threading.Thread(
            target=self._suspend_user_thread,
            args=(user_id, result_list)
        )
        thread.start()

        # Await in the event loop for the thread to complete
        # We use the sleep function to yield control back to the event loop
        # while periodically checking if the thread is done
        while thread.is_alive():
            await self.api.sleep(0.1)  # 100ms sleep to not hog the event loop

        thread.join()  # Make sure thread is completely done

        return result_list[0] if result_list else False

    async def _force_join_room(self, user_id: str, room_id: str) -> bool:
        """
        Async wrapper to run the synchronous room join function in a thread.
        """
        result_list = []
        thread = threading.Thread(
            target=self._force_join_room_thread,
            args=(user_id, room_id, result_list)
        )
        thread.start()

        # Await in the event loop for the thread to complete
        while thread.is_alive():
            await self.api.sleep(0.1)  # 100ms sleep to not hog the event loop

        thread.join()  # Make sure thread is completely done

        return result_list[0] if result_list else False
