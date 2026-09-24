"""Authorized operations portal endpoints (authentication supplied by app)."""

from functools import wraps

from flask import Blueprint, jsonify, request, session
from sqlalchemy.exc import SQLAlchemyError

from backend.extensions import db
from backend.models.operations import CourseDate, Lead, StaffEscalation


def create_operations_blueprint(operations_service, staff_required):
    blueprint = Blueprint("operations", __name__, url_prefix="/api/staff")
    service = operations_service

    def handled(function):
        @wraps(function)
        def wrapper(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except LookupError as error:
                db.session.rollback()
                return jsonify({"error": {"code": "NOT_FOUND", "message": str(error)}}), 404
            except ValueError as error:
                db.session.rollback()
                return jsonify({"error": {"code": "INVALID_OPERATION", "message": str(error)}}), 400
            except SQLAlchemyError:
                db.session.rollback()
                return jsonify({"error": {"code": "DATABASE_ERROR", "message": "The operation could not be saved."}}), 500
        return wrapper

    def payload():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object.")
        return data

    def actor():
        return str(session.get("staff_username", "staff"))

    @blueprint.get("/leads")
    @staff_required
    @handled
    def leads():
        query = Lead.query
        if request.args.get("status"):
            query = query.filter_by(status=request.args["status"])
        return jsonify({"leads": [service.serialize_lead(row) for row in query.order_by(Lead.updated_at.desc()).limit(500).all()]})

    @blueprint.patch("/leads/<lead_id>")
    @staff_required
    @handled
    def update_lead(lead_id):
        return jsonify({"lead": service.update_lead(lead_id, payload(), actor())})

    @blueprint.get("/escalations")
    @staff_required
    @handled
    def escalations():
        query = StaffEscalation.query
        if request.args.get("status"):
            query = query.filter_by(status=request.args["status"])
        return jsonify({"escalations": [service.serialize_escalation(row) for row in query.order_by(StaffEscalation.created_at.desc()).limit(500).all()]})

    @blueprint.patch("/escalations/<escalation_id>")
    @staff_required
    @handled
    def update_escalation(escalation_id):
        return jsonify({"escalation": service.update_escalation(escalation_id, payload(), actor())})

    @blueprint.get("/cases/<conversation_id>")
    @staff_required
    @handled
    def case_detail(conversation_id):
        result = service.case_detail(conversation_id)
        db.session.commit()
        return jsonify(result)

    @blueprint.post("/cases/<conversation_id>/reply")
    @staff_required
    @handled
    def staff_reply(conversation_id):
        data = payload()
        return jsonify(service.staff_reply(conversation_id, data.get("body"), data.get("subject", "Course enquiry"), actor())), 202

    @blueprint.post("/cases/<conversation_id>/assign-course-date")
    @staff_required
    @handled
    def assign_course_date(conversation_id):
        return jsonify(service.assign_course_date(conversation_id, payload().get("course_date_id"), actor()))

    @blueprint.get("/course-dates")
    @staff_required
    @handled
    def course_dates():
        return jsonify({"course_dates": [service.serialize_course_date(row) for row in CourseDate.query.order_by(CourseDate.date).all()]})

    @blueprint.post("/course-dates")
    @staff_required
    @handled
    def create_course_date():
        return jsonify({"course_date": service.save_course_date(payload(), actor=actor())}), 201

    @blueprint.patch("/course-dates/<course_date_id>")
    @staff_required
    @handled
    def update_course_date(course_date_id):
        return jsonify({"course_date": service.save_course_date(payload(), course_date_id, actor())})

    @blueprint.delete("/course-dates/<course_date_id>")
    @staff_required
    @handled
    def delete_course_date(course_date_id):
        service.delete_course_date(course_date_id, actor())
        return "", 204

    @blueprint.get("/content")
    @staff_required
    @handled
    def content():
        return jsonify({"content": service.catalogue.public_summary()})

    @blueprint.patch("/content")
    @staff_required
    @handled
    def update_content():
        return jsonify({"content": service.update_content(payload(), actor())})

    return blueprint
