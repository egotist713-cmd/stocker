from app.database.db import add_asset, add_event, get_asset, init_database


def test_database():
    init_database()

    asset_id = add_asset(
        filename="test.jpg",
        source_path="data/incoming/test.jpg",
        file_hash="test-hash-001",
        extension=".jpg",
        width=4096,
        height=3072,
        file_size=123456,
    )

    add_event(
        asset_id=asset_id,
        stage="INGEST",
        status="DONE",
        message="Test asset inserted successfully",
    )

    asset = get_asset(asset_id)

    assert asset is not None
    assert asset["filename"] == "test.jpg"
    assert asset["status"] == "NEW"
    assert asset["width"] == 4096
    assert asset["height"] == 3072

    print("Database test: OK")
    print(f"Asset ID: {asset_id}")
    print(f"Asset: {asset}")


if __name__ == "__main__":
    test_database()
