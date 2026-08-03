import time
from .config import get_settings
from .db import SessionLocal, init_db
from .service import process_next_job, seed_demo


def main() -> None:
    settings = get_settings(); init_db()
    with SessionLocal() as db:
        if settings.demo_mode: seed_demo(db)
    while True:
        with SessionLocal() as db:
            processed = process_next_job(db, settings)
        time.sleep(0.5 if processed else 3)


if __name__ == "__main__":
    main()
