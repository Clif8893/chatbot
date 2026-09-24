"""Human-labelled quality measurements; no invented production accuracy."""
from backend.extensions import db
from backend.models import new_id, utc_now


class QualityReview(db.Model):
    __tablename__ = 'quality_reviews'
    __table_args__ = (db.UniqueConstraint('kind', 'target_id', name='uq_quality_review_target'),)
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    kind = db.Column(db.String(32), nullable=False)
    target_id = db.Column(db.String(36), nullable=False)
    correct = db.Column(db.Boolean, nullable=False)
    reviewer = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utc_now, nullable=False)
