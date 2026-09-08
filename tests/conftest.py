import os
import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cad_costing_agent import db
from cad_costing_agent.db import get_session, init_db
from cad_costing_agent.main import app
from cad_costing_agent.rate_sets import SEED_RATE_SETS


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    """Give every test its own freshly-seeded in-memory database.

    Each test gets a private SQLite database held in memory. StaticPool keeps
    every connection pointed at that one database, and the `get_session`
    dependency is overridden so endpoints use it instead of the configured
    file. Estimates written by one test are therefore invisible to the next.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_db(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_session():
        with session_factory() as session:
            yield session

    # Belt and braces: point the module-level configuration at a throwaway file
    # too, so nothing can reach the real database even off the injected path.
    monkeypatch.setenv(db.DATABASE_URL_ENV_VAR, f"sqlite:///{tmp_path / 'test.db'}")
    db.reset_engine()

    app.dependency_overrides[get_session] = override_get_session
    try:
        yield session_factory
    finally:
        app.dependency_overrides.pop(get_session, None)
        engine.dispose()
        db.reset_engine()


@pytest.fixture
def db_session(isolated_database):
    """A session on the isolated test database, for repository-level tests."""
    with isolated_database() as session:
        yield session


@pytest.fixture
def seed_rate_sets_by_identity():
    """Seed rate sets keyed by (rate_set_id, version), for pure domain tests."""
    return {(rate_set.rate_set_id, rate_set.version): rate_set for rate_set in SEED_RATE_SETS}


@pytest.fixture(scope="session")
def step_box_bytes():
    """Factory producing real STEP file bytes for an axis-aligned box.

    Generated locally with OpenCASCADE's own STEP writer rather than checked
    in or downloaded, so the fixture is deterministic and its expected volume,
    surface area, and bounding box are known exactly.

    `unit` is the unit the STEP file is *authored* in ("MM", "INCH", ...). The
    box dimensions are always given in millimetres; the writer converts them,
    so a 25.4 mm box written as "INCH" produces a genuine 1-inch STEP file.
    """

    def _make(x: float = 10.0, y: float = 20.0, z: float = 30.0, unit: str = "MM") -> bytes:
        # Imported lazily so tests that never touch STEP don't pay OCP's
        # multi-second native import.
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.IFSelect import IFSelect_ReturnStatus
        from OCP.Interface import Interface_Static
        from OCP.Message import Message, Message_PrinterOStream
        from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

        # OCCT writes transfer statistics straight to the console otherwise.
        Message.DefaultMessenger_s().RemovePrinters(Message_PrinterOStream.get_type_descriptor_s())

        # `write.step.unit` is global OCCT state, so always set it explicitly
        # rather than inheriting whatever a previous test left behind.
        Interface_Static.SetCVal_s("write.step.unit", unit)

        shape = BRepPrimAPI_MakeBox(x, y, z).Shape()

        file_descriptor, temp_path = tempfile.mkstemp(suffix=".step")
        os.close(file_descriptor)
        try:
            writer = STEPControl_Writer()
            writer.Transfer(shape, STEPControl_StepModelType.STEPControl_AsIs)
            assert writer.Write(temp_path) == IFSelect_ReturnStatus.IFSelect_RetDone
            with open(temp_path, "rb") as step_file:
                return step_file.read()
        finally:
            os.unlink(temp_path)

    return _make
