# core

The wire format, addressing, and the switch: envelope encoding, Redis key
construction, the registry (who's on the tenant), policy (import/export
ACL), the two delivery channels (send/receive), and the switch daemon that
forwards a message by address without reading its payload.

Everything else is a module that plugs into this.
