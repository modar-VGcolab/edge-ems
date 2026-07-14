"""Typhoon HIL606 schematic builder + model<->Modbus signal bridge.

These modules use the Typhoon HIL Python API (typhoon.api.schematic_editor and
typhoon.api.hil). They are guarded so the package imports without the toolchain
installed (e.g. in CI / this sandbox); the build/bridge functions raise a clear
error if invoked without Typhoon. The electrical plant they build realises the
exact equations in hil.plant.models, so the software plant-in-the-loop and the
rig agree.
"""
