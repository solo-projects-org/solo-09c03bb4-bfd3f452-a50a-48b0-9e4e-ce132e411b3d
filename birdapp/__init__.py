import os

from flask import Flask


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        DATABASE=os.environ.get(
            "BIRDAPP_DB", os.path.join(os.getcwd(), "data", "birdapp.db")
        ),
        SECRET_KEY=os.environ.get("BIRDAPP_SECRET", "dev-secret-change-me"),
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,  # 上传文件上限 16MB
    )
    if test_config:
        app.config.update(test_config)

    from . import db as db_module

    db_module.init_app(app)

    from . import views

    app.register_blueprint(views.bp)
    return app
