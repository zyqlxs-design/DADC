"""Temporary compatibility helpers for AEDT Student session discovery."""

from __future__ import annotations


def enable_student_session_discovery() -> bool:
    """Make PyAEDT's local gRPC probe include ``ansysedtsv`` processes.

    PyAEDT 1.4.0 calls ``active_sessions()`` without forwarding the Student
    flag while polling a newly launched gRPC server. Consequently the Student
    executable is ignored even when it is listening. Keep the workaround
    local to DADC scripts and leave the installed package intact.

    Returns
    -------
    bool
        ``True`` when the workaround was newly installed and ``False`` when
        it was already active.
    """
    from ansys.aedt.core.generic import general_methods

    original = general_methods.active_sessions
    if getattr(original, "_dadc_student_session_discovery", False):
        return False

    def active_sessions(version=None, student_version=True, non_graphical=None):
        return original(
            version=version,
            student_version=student_version,
            non_graphical=non_graphical,
        )

    active_sessions._dadc_student_session_discovery = True
    general_methods.active_sessions = active_sessions
    return True
