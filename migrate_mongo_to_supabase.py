"""Migra colecciones MongoDB a la tabla JSONB de Supabase."""
from __future__ import annotations

import asyncio
import os

from pymongo import MongoClient

from postgres_compat import PostgresCompatDB


def _clean_doc(doc: dict) -> dict:
    clean = dict(doc)
    clean.pop("_id", None)
    return clean


async def migrate():
    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME")
    database_url = os.environ.get("DATABASE_URL")
    if not mongo_url or not db_name:
        raise RuntimeError("Faltan MONGO_URL o DB_NAME.")
    if not database_url:
        raise RuntimeError("Falta DATABASE_URL.")

    mongo = MongoClient(mongo_url)
    source_db = mongo[db_name]
    target_db = PostgresCompatDB(database_url)

    collection_names = [
        name for name in source_db.list_collection_names()
        if not name.startswith("system.")
    ]
    print(f"Colecciones encontradas: {', '.join(sorted(collection_names)) or '(ninguna)'}")

    for name in sorted(collection_names):
        docs = [_clean_doc(doc) for doc in source_db[name].find({})]
        if not docs:
            print(f"{name}: sin documentos, omitida")
            continue
        await getattr(target_db, name).delete_many({})
        await getattr(target_db, name).insert_many(docs)
        print(f"{name}: {len(docs)} documentos migrados")

    mongo.close()


if __name__ == "__main__":
    asyncio.run(migrate())
