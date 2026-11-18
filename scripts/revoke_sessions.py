"""Recovery: end every owner browser session.

Use after a lost or shared browser, then rotate OWNER_API_TOKEN in .env and
restart the API if the token itself may have been exposed.
"""

from app.api.sessions import revoke_all
from app.config import get_settings
from app.db.session import make_engine, make_session_factory


def main() -> None:
    engine = make_engine(get_settings().database_url.get_secret_value())
    try:
        count = revoke_all(make_session_factory(engine))
    finally:
        engine.dispose()
    print(f"Revoked {count} owner session(s).")


if __name__ == "__main__":
    main()
