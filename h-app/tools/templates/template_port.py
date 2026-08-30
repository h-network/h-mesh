"""Minimal reference port built only from h-mesh core's public contract."""

from collections.abc import Callable

from core.channels import receive as receive_channel
from core.channels import send as send_channel
from core.keys import prefix


class TemplatePort:
    """Wrap one participant's registry row and send/receive queue boundaries.

    A real module owns what its openers do at the far edge. The reusable bus
    mechanics remain the same: register a participant, send through egress,
    receive through ingress, and remove only that participant's scoped state.
    """

    _OWNED_RESOURCES = ("egress", "ingress", "dead", "unreplied")

    def __init__(self, r, *, pod: str, tenant: str):
        self.r = r
        self.pod = pod
        self.tenant = tenant
        self.agent: str | None = None

    def register(self, agent: str, port_type: str) -> None:
        """Publish this port's participant name and edge type."""
        if self.agent is not None:
            raise RuntimeError(f"port is already registered as {self.agent!r}")
        registry = prefix(self.pod, self.tenant, resource="registry")
        # Construct one agent key up front so invalid names fail before HSET.
        prefix(self.pod, self.tenant, agent, "egress")
        self.r.hset(registry, agent, port_type)
        self.agent = agent

    def send(
        self,
        destination: str,
        payload: dict,
        kind: str = "Message",
        *,
        correlation_id: str | None = None,
    ) -> str:
        """Send one envelope as this registered participant."""
        agent = self._registered_agent()
        return send_channel(
            self.r,
            pod=self.pod,
            tenant=self.tenant,
            source=agent,
            destination=destination,
            payload=payload,
            kind=kind,
            correlation_id=correlation_id,
            module="template-port",
        )

    def receive(
        self,
        openers: dict[str, Callable[[dict], None]],
        *,
        timeout: int = 0,
        blocking: bool = False,
    ) -> None:
        """Receive at most one envelope and dispatch it by kind."""
        agent = self._registered_agent()
        receive_channel(
            self.r,
            pod=self.pod,
            tenant=self.tenant,
            agent=agent,
            openers=openers,
            timeout=timeout,
            blocking=blocking,
            module="template-port",
        )

    def deregister(self) -> None:
        """Remove this participant's registry row and port-owned state."""
        if self.agent is None:
            return
        agent = self.agent
        registry = prefix(self.pod, self.tenant, resource="registry")
        owned_keys = [
            prefix(self.pod, self.tenant, agent, resource)
            for resource in self._OWNED_RESOURCES
        ]
        self.r.hdel(registry, agent)
        self.r.delete(*owned_keys)
        self.agent = None

    def cleanup(self) -> None:
        """Alias for idempotent teardown in a caller's ``finally`` block."""
        self.deregister()

    def _registered_agent(self) -> str:
        if self.agent is None:
            raise RuntimeError("port is not registered")
        return self.agent
