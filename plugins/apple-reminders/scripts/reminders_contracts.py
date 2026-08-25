#!/usr/bin/env python3
"""Pure SQLite schema contracts shared by the adapter and doctor.

Importing this module does not resolve user paths, open SQLite stores, or load
Apple frameworks.  The runtime profile is the adapter's exact write gate.  The
diagnostic profile is the doctor's intentionally weaker, content-free preflight;
passing it never grants runtime write approval.
"""

from __future__ import annotations

from typing import Literal


SchemaRequirements = dict[str, set[str]]
SchemaProfile = dict[str, SchemaRequirements]
SchemaBoundary = Literal["runtime", "diagnostic"]


def _fields(names: str) -> set[str]:
    return set(names.split())


def _schema(**tables: str) -> SchemaRequirements:
    return {table: _fields(columns) for table, columns in tables.items()}


def _merge(*groups: SchemaRequirements) -> SchemaRequirements:
    merged: SchemaRequirements = {}
    for group in groups:
        for table, columns in group.items():
            merged.setdefault(table, set()).update(columns)
    return merged


REQUIRED_TABLES = _fields(
    "ZREMCDREMINDER ZREMCDBASELIST ZREMCDBASESECTION ZREMCDHASHTAGLABEL "
    "ZREMCDOBJECT ZREMCKCLOUDSTATE Z_PRIMARYKEY"
)


_RUNTIME_SCHEMA_REQUIREMENTS: SchemaProfile = {
    "create_reminder_db": _schema(
        ZREMCDREMINDER="""
            ZACCOUNT ZALLDAY ZCKCLOUDSTATE ZCKDIRTYFLAGS ZCKIDENTIFIER ZCOMPLETED
            ZCREATIONDATE ZDACALENDARITEMUNIQUEIDENTIFIER ZDISPLAYDATEDATE
            ZDISPLAYDATEISALLDAY ZDISPLAYDATETIMEZONE
            ZDISPLAYDATEUPDATEDFORSECONDSFROMGMT ZDUEDATE
            ZEFFECTIVEMINIMUMSUPPORTEDAPPVERSION ZFLAGGED ZICSDISPLAYORDER
            ZIDENTIFIER ZISURGENTSTATEENABLEDFORCURRENTUSER ZLASTMODIFIEDDATE ZLIST
            ZMARKEDFORDELETION ZMINIMUMSUPPORTEDAPPVERSION ZNOTES ZNOTESDOCUMENT
            ZPRIORITY ZRESOLUTIONTOKENMAP_V3_JSONDATA ZSPOTLIGHTINDEXCOUNT
            ZTIMEZONE ZTITLE ZTITLEDOCUMENT Z_ENT Z_FOK_LIST Z_OPT Z_PK
        """,
        ZREMCDBASELIST="""
            ZACCOUNT ZCKCLOUDSTATE ZCKIDENTIFIER ZMARKEDFORDELETION ZNAME
            ZREMINDERIDSMERGEABLEORDERING_V2_JSON Z_OPT Z_PK
        """,
        ZREMCKCLOUDSTATE="""
            ZCURRENTLOCALVERSION ZLATESTVERSIONSYNCEDTOCLOUD ZLOCALVERSIONDATE
            ZREMINDER Z_ENT Z_OPT Z_PK
        """,
        Z_PRIMARYKEY="Z_ENT Z_MAX Z_NAME",
    ),
    "delete_reminder_db": _schema(
        ZREMCDREMINDER="""
            ZCKCLOUDSTATE ZCKIDENTIFIER ZLASTMODIFIEDDATE ZLIST
            ZMARKEDFORDELETION Z_FOK_LIST Z_OPT Z_PK
        """,
        ZREMCDBASELIST="""
            ZCKCLOUDSTATE ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA
            ZREMINDERIDSMERGEABLEORDERING_V2_JSON Z_OPT Z_PK
        """,
        ZREMCKCLOUDSTATE="ZCURRENTLOCALVERSION ZLOCALVERSIONDATE Z_OPT Z_PK",
    ),
    "cleanup_tags": _schema(
        ZREMCDHASHTAGLABEL="""
            ZACCOUNTIDENTIFIER ZCANONICALNAME ZNAME ZUUIDFORCHANGETRACKING Z_PK
        """,
        ZREMCDOBJECT="ZHASHTAGLABEL ZMARKEDFORDELETION Z_ENT Z_PK",
    ),
    "update_reminder_db": _schema(
        ZREMCDREMINDER="""
            ZALLDAY ZCKCLOUDSTATE ZCKIDENTIFIER ZDISPLAYDATEDATE
            ZDISPLAYDATEISALLDAY ZDISPLAYDATETIMEZONE ZDUEDATE ZFLAGGED
            ZLASTMODIFIEDDATE ZNOTES ZNOTESDOCUMENT ZPRIORITY ZTIMEZONE ZTITLE
            ZTITLEDOCUMENT Z_OPT Z_PK
        """,
        ZREMCKCLOUDSTATE="ZCURRENTLOCALVERSION ZLOCALVERSIONDATE Z_OPT Z_PK",
    ),
    "set_completion_db": _schema(
        ZREMCDREMINDER="""
            ZCKCLOUDSTATE ZCKIDENTIFIER ZCOMPLETED ZCOMPLETIONDATE
            ZLASTMODIFIEDDATE Z_OPT Z_PK
        """,
        ZREMCKCLOUDSTATE="ZCURRENTLOCALVERSION ZLOCALVERSIONDATE Z_OPT Z_PK",
    ),
    "tag_assignment_db": _schema(
        ZREMCDREMINDER="""
            ZACCOUNT ZCKCLOUDSTATE ZCKIDENTIFIER ZLASTMODIFIEDDATE Z_OPT Z_PK
        """,
        ZREMCDHASHTAGLABEL="""
            ZACCOUNTIDENTIFIER ZCANONICALNAME ZNAME ZUUIDFORCHANGETRACKING
            Z_ENT Z_OPT Z_PK
        """,
        ZREMCDOBJECT="""
            ZACCOUNT ZCKCLOUDSTATE ZCKIDENTIFIER ZHASHTAGLABEL ZIDENTIFIER
            ZMARKEDFORDELETION ZREMINDER3 Z_ENT Z_OPT Z_PK
        """,
        ZREMCKCLOUDSTATE="""
            Z13_OBJECT ZCURRENTLOCALVERSION ZLATESTVERSIONSYNCEDTOCLOUD
            ZLOCALVERSIONDATE ZOBJECT Z_ENT Z_OPT Z_PK
        """,
        Z_PRIMARYKEY="Z_ENT Z_MAX Z_NAME",
    ),
    "create_section_db": _schema(
        ZREMCDBASELIST="ZACCOUNT ZCKIDENTIFIER ZMARKEDFORDELETION ZNAME Z_PK",
        ZREMCDBASESECTION="""
            ZACCOUNT ZCKCLOUDSTATE ZCKIDENTIFIER ZCREATIONDATE ZDISPLAYNAME
            ZIDENTIFIER ZLIST ZMARKEDFORDELETION ZRESOLUTIONTOKENMAP_V3_JSONDATA
            Z_ENT Z_FOK_LIST Z_OPT Z_PK
        """,
        ZREMCKCLOUDSTATE="""
            Z5_SECTION ZCURRENTLOCALVERSION ZLATESTVERSIONSYNCEDTOCLOUD
            ZLOCALVERSIONDATE ZSECTION Z_ENT Z_OPT Z_PK
        """,
        Z_PRIMARYKEY="Z_ENT Z_MAX Z_NAME",
    ),
    "move_to_section_db": _schema(
        ZREMCDREMINDER="ZCKIDENTIFIER ZLIST ZMARKEDFORDELETION Z_OPT Z_PK",
        ZREMCDBASELIST="""
            ZCKCLOUDSTATE ZCKIDENTIFIER ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA
            ZNAME Z_OPT Z_PK
        """,
        ZREMCDBASESECTION="""
            ZCKIDENTIFIER ZDISPLAYNAME ZLIST ZMARKEDFORDELETION Z_PK
        """,
        ZREMCKCLOUDSTATE="ZCURRENTLOCALVERSION ZLOCALVERSIONDATE Z_PK",
    ),
    "attachment_mutation_db": _schema(
        ZREMCDREMINDER="""
            ZACCOUNT ZCKCLOUDSTATE ZCKIDENTIFIER ZLASTMODIFIEDDATE
            ZMARKEDFORDELETION Z_OPT Z_PK
        """,
        ZREMCDOBJECT="""
            ZACCOUNT ZCKCLOUDSTATE ZCKIDENTIFIER ZFILENAME ZFILESIZE ZHEIGHT
            ZHOSTURL ZIDENTIFIER ZMARKEDFORDELETION ZREMINDER2 ZSHA512SUM ZURL
            ZUTI ZWIDTH Z_ENT Z_FOK_REMINDER1 Z_OPT Z_PK
        """,
        ZREMCKCLOUDSTATE="""
            Z13_OBJECT ZCURRENTLOCALVERSION ZLATESTVERSIONSYNCEDTOCLOUD
            ZLOCALVERSIONDATE ZOBJECT Z_ENT Z_OPT Z_PK
        """,
        Z_PRIMARYKEY="Z_ENT Z_MAX Z_NAME",
    ),
}


_LIST_FIELDS = _schema(
    ZREMCDBASELIST="Z_PK ZCKIDENTIFIER ZNAME ZMARKEDFORDELETION"
)
_REMINDER_FIELDS = _schema(
    ZREMCDREMINDER="""
        Z_PK ZCKIDENTIFIER ZTITLE ZLIST ZCOMPLETED ZMARKEDFORDELETION
    """
)
_SECTION_FIELDS = _schema(
    ZREMCDBASESECTION="""
        Z_PK ZCKIDENTIFIER ZDISPLAYNAME ZLIST ZMARKEDFORDELETION
    """
)
_TAG_FIELDS = _schema(
    ZREMCDHASHTAGLABEL="Z_PK ZNAME ZCANONICALNAME",
    ZREMCDOBJECT="""
        Z_PK Z_ENT ZHASHTAGLABEL ZREMINDER3 ZMARKEDFORDELETION
    """,
)
_ATTACHMENT_FIELDS = _schema(
    ZREMCDOBJECT="""
        Z_PK Z_ENT ZCKIDENTIFIER ZREMINDER2 Z_FOK_REMINDER1 ZMARKEDFORDELETION
    """
)
_ATTACHMENT_SYNC_FIELDS = _schema(
    ZREMCDOBJECT="ZCKSERVERRECORDDATA",
    ZREMCKCLOUDSTATE="""
        ZINCLOUD ZCURRENTLOCALVERSION ZLATESTVERSIONSYNCEDTOCLOUD
    """,
)
_CLOUD_WRITE_FIELDS = _schema(
    ZREMCKCLOUDSTATE="Z_PK ZCURRENTLOCALVERSION ZLOCALVERSIONDATE",
    Z_PRIMARYKEY="Z_ENT Z_MAX",
)
_REMINDER_WRITE_FIELDS = _schema(
    ZREMCDREMINDER="""
        Z_PK Z_OPT ZCKIDENTIFIER ZCKCLOUDSTATE ZLASTMODIFIEDDATE ZLIST
        ZMARKEDFORDELETION
    """
)


_DIAGNOSTIC_SCHEMA_REQUIREMENTS: SchemaProfile = {
    "list_lists": _merge(_LIST_FIELDS),
    "list_sections": _merge(_LIST_FIELDS, _SECTION_FIELDS),
    "snapshot": _merge(
        _LIST_FIELDS,
        _REMINDER_FIELDS,
        _SECTION_FIELDS,
        _TAG_FIELDS,
        _ATTACHMENT_FIELDS,
    ),
    "search_reminders": _merge(_LIST_FIELDS, _REMINDER_FIELDS),
    "read_reminder": _merge(
        _LIST_FIELDS, _REMINDER_FIELDS, _TAG_FIELDS, _ATTACHMENT_FIELDS
    ),
    "list_tags": _merge(_TAG_FIELDS),
    "cache_rebuild": _merge(
        _LIST_FIELDS,
        _REMINDER_FIELDS,
        _SECTION_FIELDS,
        _TAG_FIELDS,
        _ATTACHMENT_FIELDS,
    ),
    "create_list_db": _merge(_LIST_FIELDS, _CLOUD_WRITE_FIELDS),
    "create_reminder_db": _merge(
        _LIST_FIELDS, _REMINDER_FIELDS, _REMINDER_WRITE_FIELDS, _CLOUD_WRITE_FIELDS
    ),
    "update_reminder_db": _merge(
        _LIST_FIELDS, _REMINDER_FIELDS, _REMINDER_WRITE_FIELDS, _CLOUD_WRITE_FIELDS
    ),
    "complete_reminder_db": _merge(
        _LIST_FIELDS, _REMINDER_FIELDS, _REMINDER_WRITE_FIELDS, _CLOUD_WRITE_FIELDS
    ),
    "delete_reminder_db": _schema(
        ZREMCDREMINDER="""
            Z_PK Z_OPT ZCKIDENTIFIER ZCKCLOUDSTATE ZLASTMODIFIEDDATE ZLIST
            Z_FOK_LIST ZMARKEDFORDELETION
        """,
        ZREMCDBASELIST="""
            Z_PK Z_OPT ZCKCLOUDSTATE ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA
        """,
        ZREMCKCLOUDSTATE="Z_PK ZCURRENTLOCALVERSION ZLOCALVERSIONDATE",
    ),
    "create_section_db": _merge(
        _LIST_FIELDS, _SECTION_FIELDS, _CLOUD_WRITE_FIELDS
    ),
    "move_to_section_db": _merge(
        _LIST_FIELDS, _REMINDER_FIELDS, _SECTION_FIELDS, _CLOUD_WRITE_FIELDS
    ),
    "add_tag_db": _merge(
        _REMINDER_FIELDS, _REMINDER_WRITE_FIELDS, _TAG_FIELDS, _CLOUD_WRITE_FIELDS
    ),
    "remove_tag_db": _merge(
        _REMINDER_FIELDS, _REMINDER_WRITE_FIELDS, _TAG_FIELDS, _CLOUD_WRITE_FIELDS
    ),
    "cleanup_tags": _schema(
        ZREMCDHASHTAGLABEL="""
            Z_PK ZNAME ZCANONICALNAME ZACCOUNTIDENTIFIER ZUUIDFORCHANGETRACKING
        """,
        ZREMCDOBJECT="Z_PK Z_ENT ZHASHTAGLABEL ZMARKEDFORDELETION",
    ),
    "attach_image_db": _merge(
        _REMINDER_FIELDS,
        _REMINDER_WRITE_FIELDS,
        _ATTACHMENT_FIELDS,
        _CLOUD_WRITE_FIELDS,
    ),
    "attach_image_reminderkit_verify": _merge(
        _REMINDER_FIELDS, _ATTACHMENT_FIELDS, _ATTACHMENT_SYNC_FIELDS
    ),
    "attach_url_db": _merge(
        _REMINDER_FIELDS,
        _REMINDER_WRITE_FIELDS,
        _ATTACHMENT_FIELDS,
        _CLOUD_WRITE_FIELDS,
    ),
    "list_attachments": _merge(_REMINDER_FIELDS, _ATTACHMENT_FIELDS),
    "audit_attachments": _merge(_REMINDER_FIELDS, _ATTACHMENT_FIELDS),
    "repair_attachments": _merge(
        _REMINDER_FIELDS,
        _REMINDER_WRITE_FIELDS,
        _ATTACHMENT_FIELDS,
        _ATTACHMENT_SYNC_FIELDS,
        _CLOUD_WRITE_FIELDS,
    ),
    "delete_attachment_db": _merge(
        _REMINDER_FIELDS,
        _REMINDER_WRITE_FIELDS,
        _ATTACHMENT_FIELDS,
        _CLOUD_WRITE_FIELDS,
    ),
    "replace_attachment_db": _merge(
        _REMINDER_FIELDS,
        _REMINDER_WRITE_FIELDS,
        _ATTACHMENT_FIELDS,
        _CLOUD_WRITE_FIELDS,
    ),
}


# One registry owns both safety boundaries.  This also records the otherwise
# easy-to-miss relationship between public diagnostic names and runtime gates.
SQLITE_COMMAND_SCHEMA_CONTRACT: dict[SchemaBoundary, SchemaProfile] = {
    "runtime": _RUNTIME_SCHEMA_REQUIREMENTS,
    "diagnostic": _DIAGNOSTIC_SCHEMA_REQUIREMENTS,
}

DIAGNOSTIC_RUNTIME_ALIASES = {
    "create_reminder_db": "create_reminder_db",
    "update_reminder_db": "update_reminder_db",
    "complete_reminder_db": "set_completion_db",
    "delete_reminder_db": "delete_reminder_db",
    "create_section_db": "create_section_db",
    "move_to_section_db": "move_to_section_db",
    "add_tag_db": "tag_assignment_db",
    "remove_tag_db": "tag_assignment_db",
    "cleanup_tags": "cleanup_tags",
    "attach_image_db": "attachment_mutation_db",
    "attach_url_db": "attachment_mutation_db",
    "delete_attachment_db": "attachment_mutation_db",
    "replace_attachment_db": "attachment_mutation_db",
}


def command_schema_requirements(boundary: SchemaBoundary) -> SchemaProfile:
    """Return the authoritative requirements for one validation boundary."""

    return {
        command: {table: set(columns) for table, columns in requirements.items()}
        for command, requirements in SQLITE_COMMAND_SCHEMA_CONTRACT[boundary].items()
    }
