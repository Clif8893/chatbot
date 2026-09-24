"""Staff sampling of the four proposal KPIs."""
from flask import jsonify, request, session
from backend.extensions import db
from backend.models import Message
from backend.models.finance import FinancialDocument, Payment
from backend.models.quality import QualityReview
from backend.services.security import staff_required


def register_quality(app):
    models = {'router': Message, 'faq': Message, 'document': FinancialDocument, 'payment': Payment}

    @app.post('/api/staff/quality/reviews')
    @staff_required(roles=('admin', 'accountant', 'operations'))
    def review():
        body = request.get_json(silent=True) or {}
        kind, target_id, correct = body.get('kind'), body.get('target_id'), body.get('correct')
        if kind not in models or not isinstance(target_id, str) or type(correct) is not bool:
            return jsonify(error={'message': 'Provide kind, target_id and a boolean correct judgement.'}), 400
        target = db.session.get(models[kind], target_id)
        if not target:
            return jsonify(error={'message': 'Review target not found.'}), 404
        if kind in {'document', 'payment'} and session['staff_role'] not in {'admin', 'accountant'}:
            return jsonify(error={'message': 'Accounts access required.'}), 403
        if (kind == 'router' and (target.role != 'user' or not target.detected_intent)) or (kind == 'faq' and target.agent_name != 'faq_agent') or (kind == 'payment' and target.confirmed_by != 'vision_policy'):
            return jsonify(error={'message': 'Target does not belong to this KPI sample.'}), 400
        row = db.session.execute(db.select(QualityReview).where(QualityReview.kind == kind, QualityReview.target_id == target_id)).scalar_one_or_none()
        if row is None:
            row = QualityReview(kind=kind, target_id=target_id)
            db.session.add(row)
        row.correct, row.reviewer = correct, session['staff_username']
        db.session.commit()
        return jsonify(id=row.id), 201

    @app.get('/api/staff/quality')
    @staff_required
    def summary():
        result = {}
        for kind, model in models.items():
            # Deleted retention targets do not contribute stale quality samples.
            rows = db.session.execute(db.select(QualityReview).join(model, model.id == QualityReview.target_id).where(QualityReview.kind == kind)).scalars().all()
            total = len(rows)
            incorrect = sum(not row.correct for row in rows)
            result[kind] = {'sample_count': total, 'rate': ((incorrect if kind == 'payment' else total - incorrect) / total) if total else None,
                            'metric': 'false_match_rate' if kind == 'payment' else 'accuracy', 'target': 0 if kind == 'payment' else 1 if kind == 'document' else .95}
        return jsonify(metrics=result)
