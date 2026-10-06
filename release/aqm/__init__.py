"""agent-quota-maximizer -- see ../../AGENTS.md.

The package re-exports every public name, so `import aqm` still gives the flat
surface that `test/`, `lab/` and the notebooks use. One module per stage:

    core        Constants, paths, and the parameter table.
    io          Reading and writing our own files, and turning values into text.
    history     Questions asked of the ingested table.
    s1_ingest   Stage 1 -- ../../design/01_ingestion/DESIGN.md.
    s2_predict  Stage 2 -- ../../design/02_prediction/DESIGN.md.
    s3_budget   Stage 3 -- ../../design/03_budgeting/DESIGN.md.
    plumbing    Locks, logging, metrics and housekeeping -- ../../design/07_pipeline/DESIGN.md 4, 5, 8.
    pipeline    Every built stage in one process -- ../../design/07_pipeline/DESIGN.md 2.
    cli         Argument parsing and dispatch.
"""

from .core import (  # noqa: F401
    AGENTS,
    CONFIG,
    FAKE_TOLERANCE,
    FIVE_HOURS,
    P,
    Reading,
    SLOT_SECONDS,
    SMALL_TAIL,
    TAIL_START,
    agents,
    config_hash,
    config_path,
    data_dir,
    home,
    load_config,
    state_dir,
    usage_dir)

from .io import (  # noqa: F401
    METER_COLUMNS,
    METER_TYPES,
    SLOT_COLUMNS,
    SLOT_TYPES,
    append,
    bot_sessions,
    cell,
    clock,
    last_slot_end,
    lasting,
    latest_artifact,
    load_meter,
    meter_path,
    meter_row,
    read_all,
    read_csv,
    shown,
    slots_path,
    tail_csv,
    typed,
    write_artifact)

from .history import (  # noqa: F401
    burn_rate,
    meter_now,
    recent_slots,
    window_state)

from .s1_ingest import (  # noqa: F401
    build_slots,
    drop_stale_readings,
    ingest,
    normalise,
    read_tail,
    window_end)

from .s2_predict import (  # noqa: F401
    predict,
    predict_agent,
    predict_table)

from .s3_budget import (  # noqa: F401
    budget,
    budget_agent,
    fill_from_the_end,
    max_spend,
    max_spend_parts,
    remaining_windows)

from .plumbing import (  # noqa: F401
    acquire,
    fresh_enough,
    housekeeping,
    log,
    metric,
    rotate)

from .pipeline import (  # noqa: F401
    budget_table,
    pipeline,
    when)

from .cli import (  # noqa: F401
    REFUSED,
    main)
