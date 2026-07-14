"""External-EMS takeover gateway (HIL Scenario 1).

An external EMS publishes PCC active-power setpoints over MQTT. While those
messages are fresh the gateway *follows* them (forwarding to the controller's
live POST /setpoint); if they go silent past a watchdog timeout the gateway
*takes over* and runs self-consumption (PCC target = 0); when fresh external
messages return it *releases* control back to the external EMS.

Everything here runs in its own container (compose profile `ext-ems`); the only
existing-code change is the controller's POST /setpoint endpoint.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
