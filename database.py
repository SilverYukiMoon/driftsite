from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func
from databases import Database

DATABASE_URL = "sqlite+aiosqlite:///./permit_applications.db"

database = Database(DATABASE_URL)
Base = declarative_base()

class PermitApplication(Base):
    __tablename__ = "permit_applications"

    id = Column(Integer, primary_key=True, index=True)
    full_name = Column(String, nullable=False, index=True)
    alias = Column(String, nullable=True)
    crew = Column(String, nullable=True)
    contact_address = Column(String, nullable=True)
    preferred_contact = Column(String, nullable=True)
    other_corr_text = Column(String, nullable=True)
    permit_type = Column(String, nullable=False, index=True)
    other_permit_text = Column(String, nullable=True)
    permit_details = Column(Text, nullable=True)
    supporting_files = Column(Text, nullable=True)  # To store uploaded file paths  # JSON string list of uploaded filenames
    applicant_signature = Column(String, nullable=False)
    application_date = Column(DateTime, nullable=False)
    submitted_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"<PermitApplication(id={self.id}, full_name='{self.full_name}', permit_type='{self.permit_type}')>"