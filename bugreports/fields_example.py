"""Which parts of a report become columns. This is the one file to edit when a field is added.

Made-up names with the same layout as the real list. Copy to fields.py and put the real names there,
fields.py is not in git."""

# columns the batch file must have
FILE_COLUMNS = {
    "id": "VARCHAR",
    "created_at": "TIMESTAMPTZ",
    "updated_at": "TIMESTAMPTZ",
    "device_id": "VARCHAR",
    "archive_path": "VARCHAR",
    "device_time": "TIMESTAMPTZ",
    "device_kind": "VARCHAR",
    "summary": "VARCHAR",
    "in_use": "BOOLEAN",
}
ID = "id"
JSON_COLUMN = "summary"
SERVER_CLOCK = "updated_at"   # decides the day folder, device clocks cannot be trusted
DEVICE_CLOCK = "device_time"

# hot column <- file column
FILE_FIELDS = {
    "id": "id",
    "received_at": "updated_at",
    "first_seen_at": "created_at",
    "device_id": "device_id",
    "device_kind": "device_kind",
    "archive_path": "archive_path",
    "device_time": "device_time",
    "in_use": "in_use",
}
SORT_BY = ("device_id", "received_at")

# hot column <- (path inside the json, type). A number in the path picks an item of a list.
JSON_FIELDS = {
    "model": (("info", "Model"), "VARCHAR"),
    "os_version": (("info", "OS Version"), "VARCHAR"),
    "restart_reason": (("info", "Restart Reason"), "VARCHAR"),
    "battery_drain": (("info", "Drain"), "DOUBLE"),
    "err_kernel": (("errors", "Kernel"), "BIGINT"),
    "err_system": (("errors", "System"), "BIGINT"),
    "net_state": (("networks", 0, "state"), "VARCHAR"),
}

# text like "2 weeks, 3 days, 4 hours, 5 minutes" or "3d 4h 5m", stored as minutes
DURATIONS = {
    "uptime_min": ("info", "Uptime"),
}

# hot column <- list inside the json, stored as how many items it has
LIST_COUNTS = {
    "n_usb": ("usb",),
    "n_time_events": ("time_sync", "Events"),
}
