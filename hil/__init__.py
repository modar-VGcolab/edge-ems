"""Controller-Hardware-in-the-Loop (CHIL) rig for the edge-EMS (Gate G3).

The plant side of the controller: a Typhoon HIL606 schematic plus the Modbus TCP
servers it exposes, the profile-injection scripts that stimulate it, and the
orchestration that reuses the SIL scenario oracle (tests/sil/scenarios.py)
against the live `control` measurement.

Guiding principle -- *parity by construction*: every HIL Modbus server is
generated from the same register-map file core.py reads (hil.servers), so the
controller cannot tell the simulator from the schematic. The plant physics
(hil.plant) is pure and Typhoon-independent so it can be (a) unit-tested here,
(b) run as a software plant-in-the-loop to validate the CHIL logic without the
rig, and (c) transcribed 1:1 into the Typhoon schematic (hil.schematic).

See hil/README.md for the architecture and the firmware-verification caveat that
gates the actual G3 sign-off.
"""
