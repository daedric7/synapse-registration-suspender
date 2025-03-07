# synapse-registration-suspender
Synapse module that suspends newly registered accounts and joins them to given room.

Include in homeserver.yaml with this config:

```
modules:
  - module: reg_module.RegistrationMonitor
    config:
      #Room where messages will be sent and new users auto joined
      notification_room: "!room_id:example.com"

      #Disable to not suspend users
      suspend_users: true

      #User with admin privileges on the server 
      admin_user: "@adminuser:example.com"

      #Message will be logged in synapse logs
      reason: "Account suspended pending manual review"

      #Required admin token with admin privileges
      admin_token: "syt_admin_token"

      # URL to your synapse server
      homeserver_url: "https://example.com"

      # Enable to force join he users to the notification room
      force_join_room: true
```
