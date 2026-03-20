from fastapi.testclient import TestClient
from .main import app

client = TestClient(app)


def test_delete_template():
    # Step 1: Create a new template to ensure one exists to delete
    response = client.post(
        "/api/templates",
        json={
            "name": "Test Template",
            "description": "A template for testing",
            "system_prompt": "Test prompt",
            "allowed_tools": []
        }
    )
    assert response.status_code == 201
    template_id = response.json()["id"]

    # Step 2: Confirm that the template is present
    response = client.get("/api/templates")
    assert response.status_code == 200
    templates = response.json()
    assert any(t["id"] == template_id for t in templates)

    # Step 3: Attempt to delete the template
    response = client.delete(f"/api/templates/{template_id}")
    assert response.status_code == 200

    # Step 4: Confirm that the template has been deleted
    response = client.get("/api/templates")
    assert response.status_code == 200
    templates = response.json()
    assert not any(t["id"] == template_id for t in templates)
