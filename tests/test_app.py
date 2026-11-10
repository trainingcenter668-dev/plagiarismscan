import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import app as backend


class PlagiaScanTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        backend.DB_PATH = Path(self.temp_dir.name) / "test.sqlite3"
        backend.API_KEY = ""
        self.original_admin_initial_password = backend.ADMIN_INITIAL_PASSWORD
        backend.ADMIN_INITIAL_PASSWORD = "test-initial-admin-password"
        self.client = backend.app.test_client()

    def tearDown(self):
        backend.ADMIN_INITIAL_PASSWORD = self.original_admin_initial_password
        self.temp_dir.cleanup()

    def register_user(self, client=None, username="testuser", email="test@example.com", password="user-password-123"):
        return (client or self.client).post("/api/auth/register", json={
            "username": username, "email": email, "password": password
        })

    def login_admin(self, client=None, identifier="superadmin", password="test-initial-admin-password"):
        return (client or self.client).post("/api/auth/admin-login", json={
            "identifier": identifier, "password": password
        })

    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ok")

    def test_favicon_serves_nbits_logo(self):
        for path in ("/favicon.ico", "/static/favicon.ico"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, "image/x-icon")
                self.assertEqual(response.data[:4], b"\x00\x00\x01\x00")
                response.close()

    def test_extract_txt(self):
        payload = {"file": (io.BytesIO(b"This is a readable text file with enough words."), "sample.txt")}
        response = self.client.post("/extract", data=payload, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 200)
        self.assertIn("readable text file", response.get_json()["text"])

    def test_reject_unsupported_file(self):
        payload = {"file": (io.BytesIO(b"hello"), "sample.exe")}
        response = self.client.post("/extract", data=payload, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 415)

    def test_reference_library_add_list_delete(self):
        text = b"This reference document contains enough words for the local plagiarism reference library."
        response = self.client.post(
            "/reference-documents",
            data={"file": (io.BytesIO(text), "reference.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        document_id = response.get_json()["id"]
        self.assertEqual(len(self.client.get("/reference-documents").get_json()), 1)
        duplicate = self.client.post(
            "/reference-documents",
            data={"file": (io.BytesIO(text), "duplicate.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(duplicate.status_code, 409)
        deleted = self.client.delete(f"/reference-documents/{document_id}")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(len(self.client.get("/reference-documents").get_json()), 0)

    @patch.object(backend, "scan_document")
    def test_scan_history_and_stats(self, mocked_scan):
        mocked_scan.return_value = {
            "similarity": 25, "matches": [], "sentences": 2, "engine": "tfidf",
            "sources_checked": 1, "source_counts": {"local": 1, "web": 0, "academic": 0},
            "warnings": [],
        }
        self.assertEqual(self.register_user().status_code, 201)
        response = self.client.post("/scan", json={"text": "This is a sufficiently long sample document with more than eight words.", "name": "sample.txt"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["similarity"], 25)
        self.assertEqual(len(self.client.get("/history").get_json()), 1)
        self.assertEqual(self.client.get(f"/history/{response.get_json()['id']}").status_code, 200)
        self.assertEqual(self.client.get("/stats").get_json()["total"], 1)

    def test_registration_creates_only_normal_hashed_user(self):
        response = self.register_user()
        self.assertEqual(response.status_code, 201)
        data = response.get_json()
        self.assertEqual(data["user"]["role"], "user")
        self.assertNotIn("password", data)
        self.assertNotIn("password_hash", data)
        with backend.db_session() as conn:
            row = conn.execute("SELECT role,password_hash FROM users WHERE username='testuser'").fetchone()
        self.assertEqual(row["role"], "user")
        self.assertNotEqual(row["password_hash"], "user-password-123")
        self.assertTrue(backend.check_password_hash(row["password_hash"], "user-password-123"))
        self.assertEqual(self.register_user(username="superadmin", email="other@example.com").status_code, 400)

    def test_user_admin_login_separation_and_admin_route_protection(self):
        direct_admin = self.client.get("/admin/dashboard")
        self.assertEqual(direct_admin.status_code, 302)
        direct_admin.close()
        self.assertEqual(self.client.get("/stats").status_code, 401)
        self.assertEqual(self.login_admin(identifier="admin@yourdomain.com").status_code, 200)
        with self.client:
            self.assertEqual(self.client.get("/api/auth/session").get_json()["user"]["role"], "admin")
            admin_dashboard = self.client.get("/admin/dashboard")
            self.assertEqual(admin_dashboard.status_code, 200)
            admin_dashboard.close()
            self.assertEqual(self.client.get("/stats").status_code, 200)
            logout = self.client.post("/api/auth/logout")
            self.assertEqual(logout.status_code, 200)
            logged_out_dashboard = self.client.get("/admin/dashboard")
            self.assertEqual(logged_out_dashboard.status_code, 302)
            logged_out_dashboard.close()
        self.assertEqual(self.register_user().status_code, 201)
        self.assertEqual(
            self.client.post("/api/auth/admin-login", json={
                "identifier": "testuser", "password": "user-password-123"
            }).status_code,
            401,
        )
        self.assertEqual(
            self.client.post("/api/auth/login", json={
                "identifier": "superadmin", "password": "test-initial-admin-password"
            }).status_code,
            401,
        )
        self.client.post("/api/auth/logout")
        user_login = self.client.post("/api/auth/login", json={
            "identifier": "test@example.com", "password": "user-password-123"
        })
        self.assertEqual(user_login.status_code, 200)
        self.assertEqual(user_login.get_json()["user"]["role"], "user")
        user_admin_dashboard = self.client.get("/admin/dashboard")
        self.assertEqual(user_admin_dashboard.status_code, 302)
        user_admin_dashboard.close()
        user_dashboard = self.client.get("/dashboard")
        self.assertEqual(user_dashboard.status_code, 200)
        user_dashboard.close()

    def test_admin_login_rejects_invalid_password(self):
        response = self.client.post("/api/auth/admin-login", json={
            "identifier": "superadmin",
            "password": "not-the-admin-password",
        })
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json(), {"error": "Invalid credentials for this login."})
        self.assertFalse(self.client.get("/api/auth/session").get_json()["authenticated"])

    def test_user_scan_history_is_limited_to_owner(self, mocked_scan=None):
        with patch.object(backend, "scan_document", return_value={
            "similarity": 25, "matches": [{"text": "Matching text", "source": "Source"}],
            "sentences": 2, "engine": "tfidf", "sources_checked": 1,
            "source_counts": {"local": 1, "web": 0, "academic": 0}, "warnings": [],
        }):
            owner = self.client
            self.assertEqual(self.register_user(owner, "owner1", "owner1@example.com").status_code, 201)
            scan = owner.post("/scan", json={
                "text": "This is a sufficiently long sample document with more than eight words.",
                "name": "owned.txt",
            }).get_json()
            other = backend.app.test_client()
            self.assertEqual(self.register_user(other, "owner2", "owner2@example.com").status_code, 201)
            self.assertEqual(len(other.get("/history").get_json()), 0)
            self.assertEqual(other.get(f"/history/{scan['id']}").status_code, 404)
            self.assertEqual(owner.get(f"/history/{scan['id']}").status_code, 200)
            admin = backend.app.test_client()
            self.assertEqual(self.login_admin(admin).status_code, 200)
            self.assertEqual(len(admin.get("/history").get_json()), 1)

    def test_admin_can_change_password(self):
        self.assertEqual(self.login_admin().status_code, 200)
        response = self.client.post("/api/auth/password", json={
            "current_password": "test-initial-admin-password",
            "new_password": "new-strong-admin-password-123",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/admin/dashboard").status_code, 302)
        self.assertEqual(self.login_admin(password="new-strong-admin-password-123").status_code, 200)
        with backend.db_session() as conn:
            admin = conn.execute("SELECT password_hash FROM users WHERE role='admin'").fetchone()
        self.assertTrue(backend.check_password_hash(admin["password_hash"], "new-strong-admin-password-123"))
        self.assertNotIn("new-strong-admin-password-123", admin["password_hash"])

    def test_scan_rejects_short_text(self):
        response = self.client.post("/scan", json={"text": "too short"})
        self.assertEqual(response.status_code, 400)

    def test_contact_rejects_invalid_details(self):
        response = self.client.post("/contact", json={
            "name": "Student", "email": "not-an-email", "message": "Please help me revise this."
        })
        self.assertEqual(response.status_code, 400)

    def test_contact_reports_missing_email_configuration(self):
        with patch.dict(backend.os.environ, {"CONTACT_ADMIN_EMAIL": "", "SMTP_HOST": ""}):
            response = self.client.post("/contact", json={
                "name": "Student", "email": "student@example.com", "message": "Please help me revise this."
            })
        self.assertEqual(response.status_code, 503)

    def test_contact_sends_request_to_configured_admin(self):
        smtp_context = unittest.mock.MagicMock()
        smtp = smtp_context.__enter__.return_value
        smtp.send_message.return_value = {}
        configuration = {
            "CONTACT_ADMIN_EMAIL": "admin@example.com",
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "587",
            "SMTP_USERNAME": "mailer@example.com",
            "SMTP_PASSWORD": "smtp-password",
            "SMTP_FROM": "PlagiaScan <mailer@example.com>",
            "SMTP_STARTTLS": "1",
        }
        with patch.dict(backend.os.environ, configuration), \
             patch.object(backend.smtplib, "SMTP", return_value=smtp_context) as smtp_factory:
            response = self.client.post("/contact", json={
                "name": "Student Name",
                "email": "student@example.com",
                "document": "Essay.docx",
                "message": "Please help me revise the introduction.",
            })

        self.assertEqual(response.status_code, 200)
        smtp_factory.assert_called_once_with("smtp.example.com", 587, timeout=15)
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("mailer@example.com", "smtp-password")
        sent_message = smtp.send_message.call_args.args[0]
        self.assertEqual(sent_message["To"], "admin@example.com")
        self.assertEqual(sent_message["Reply-To"], "student@example.com")
        self.assertIn("Essay.docx", sent_message.get_content())
        self.assertIn("Please help me revise the introduction.", sent_message.get_content())

    def test_similarity_engine_finds_exact_sentence(self):
        source_sentence = "Machine learning systems analyze textual patterns and compare document content."
        text = source_sentence + " Another sentence explains the system design in detail for readers."
        result = backend.analyze_text(text, [{
            "url": "https://example.org/article",
            "title": "Example article",
            "type": "web",
            "sentences": [source_sentence],
        }])
        self.assertGreater(result["similarity"], 0)
        self.assertEqual(result["matches"][0]["source"], "Example article")

    def test_exact_copy_scores_high(self):
        source = ("Photosynthesis is the process by which green plants convert sunlight into chemical energy. "
                  "Chlorophyll absorbs light mainly in the blue and red wavelengths of the spectrum.")
        result = backend.analyze_text(source, [{
            "url": "https://example.org/bio", "title": "Biology", "type": "web",
            "sentences": backend.split_sentences(source)}])
        self.assertGreaterEqual(result["similarity"], 90)

    def test_source_split_across_lines_still_matches(self):
        doc = "Photosynthesis is the process by which green plants convert sunlight into chemical energy."
        page = "Photosynthesis is the process by which\ngreen plants convert sunlight\ninto chemical energy.\nUnrelated footer line here today."
        result = backend.analyze_text(doc, [{"url": "https://x.org", "title": "X", "type": "web",
                                             "sentences": backend.split_sentences(page)}])
        self.assertGreater(result["similarity"], 50)

    def test_unrelated_text_scores_zero(self):
        result = backend.analyze_text("Quantum entanglement links particles across large distances instantly.", [{
            "url": "u", "title": "Cooking", "type": "web",
            "sentences": ["Boil the pasta in salted water for ten minutes before draining."]}])
        self.assertEqual(result["similarity"], 0)

    def test_full_scan_pipeline_with_mocked_providers(self):
        doc = ("The mitochondria is the organelle that generates most of the chemical energy needed by the cell. "
               "Cells with high energy demand contain many mitochondria working together.")
        wiki = {"url": "https://en.wikipedia.org/wiki/Mitochondrion", "title": "Wikipedia: Mitochondrion",
                "type": "web", "text": doc, "content_kind": "full_text"}
        with patch.object(backend, "search_wikipedia", return_value=[wiki]), \
             patch.object(backend, "search_duckduckgo", return_value=[]), \
             patch.object(backend, "search_keyed_web", return_value=[]), \
             patch.object(backend, "search_arxiv", return_value=[]), \
             patch.object(backend, "search_openalex", return_value=[]), \
             patch.object(backend, "search_crossref", return_value=[]):
            result = backend.scan_document(doc)
        self.assertGreaterEqual(result["similarity"], 90)
        self.assertEqual(result["source_counts"]["web"], 1)

    def test_local_reference_library_detects_copy(self):
        text = b"Renewable energy sources such as solar and wind reduce greenhouse gas emissions significantly."
        self.client.post("/reference-documents", data={"file": (io.BytesIO(text), "ref.txt")},
                         content_type="multipart/form-data")
        with patch.object(backend, "discover_candidates", return_value=([], {})):
            response = self.client.post("/scan", json={"text": text.decode(), "name": "t.txt"})
        self.assertGreaterEqual(response.get_json()["similarity"], 90)


if __name__ == "__main__":
    unittest.main(verbosity=2)
