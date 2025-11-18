import os
from typing import List, Optional, Any, Dict

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from database import db, create_document, get_documents

app = FastAPI(title="ExamAi API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------
# Utility
# -----------------------

def serialize_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    if not doc:
        return doc
    d = dict(doc)
    if "_id" in d:
        d["id"] = str(d.pop("_id"))
    # Convert nested ObjectIds if any exist in simple arrays (best-effort)
    for k, v in list(d.items()):
        try:
            from bson import ObjectId  # type: ignore
            if isinstance(v, ObjectId):
                d[k] = str(v)
            if isinstance(v, list):
                d[k] = [str(x) if isinstance(x, ObjectId) else x for x in v]
        except Exception:
            pass
    return d


# -----------------------
# Models
# -----------------------

class Question(BaseModel):
    prompt: str
    type: str = Field(default="short_answer")  # short_answer | multiple_choice | essay
    options: Optional[List[str]] = None
    answer_key: Optional[Any] = None
    points: int = 1


class AssessmentCreate(BaseModel):
    title: str
    description: Optional[str] = None
    source_type: Optional[str] = Field(default=None, description="pdf|ppt|image|text")
    source_reference: Optional[str] = None
    questions: List[Question] = Field(default_factory=list)


class Assessment(AssessmentCreate):
    id: Optional[str] = None


class SubmissionAnswer(BaseModel):
    question_index: int
    answer: Any


class SubmissionCreate(BaseModel):
    assessment_id: str
    student_name: Optional[str] = None
    answers: List[SubmissionAnswer]


class GradeResult(BaseModel):
    graded: bool
    total_points: int
    score: float
    feedback: List[Dict[str, Any]]


class LessonBlock(BaseModel):
    kind: str  # text | quiz | image | video
    content: Dict[str, Any]


class LessonCreate(BaseModel):
    title: str
    description: Optional[str] = None
    content_blocks: List[LessonBlock] = Field(default_factory=list)


# -----------------------
# Root + Health
# -----------------------

@app.get("/")
def read_root():
    return {"message": "ExamAi Backend running"}


@app.get("/test")
def test_database():
    info = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set",
        "database_name": "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set",
        "connection_status": "Not Connected",
        "collections": [],
    }
    try:
        if db is not None:
            info["database"] = "✅ Connected"
            info["connection_status"] = "Connected"
            try:
                info["collections"] = db.list_collection_names()
                info["database"] = "✅ Connected & Working"
            except Exception as e:
                info["database"] = f"⚠️ Connected but error: {str(e)[:60]}"
    except Exception as e:
        info["database"] = f"❌ Error: {str(e)[:60]}"
    return info


# -----------------------
# Assessments
# -----------------------

@app.post("/assessments", response_model=Assessment)
def create_assessment(payload: AssessmentCreate):
    data = payload.dict()
    inserted_id = create_document("assessment", data)
    # Fetch created to return
    doc = db["assessment"].find_one({"_id": __import__("bson").ObjectId(inserted_id)}) if hasattr(__import__("bson"), "ObjectId") else None
    return Assessment(id=inserted_id, **data)


@app.get("/assessments")
def list_assessments():
    docs = get_documents("assessment")
    return [serialize_doc(d) for d in docs]


@app.get("/assessments/{assessment_id}")
def get_assessment(assessment_id: str):
    from bson import ObjectId  # type: ignore
    doc = db["assessment"].find_one({"_id": ObjectId(assessment_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Assessment not found")
    return serialize_doc(doc)


@app.post("/assessments/from-upload", response_model=Assessment)
def create_assessment_from_upload(
    title: str = "Generated Assessment",
    description: str = "Generated from uploaded source",
    source_type: str = "file",
    file: UploadFile = File(...),
):
    # Stubbed parser: in real app, extract text from PDF/PPT, OCR images, and generate questions
    filename = file.filename or "upload"
    fake_questions = [
        Question(
            prompt=f"Describe the main concept from {filename}.",
            type="short_answer",
            points=5,
        ),
        Question(
            prompt="Which option best matches the definition?",
            type="multiple_choice",
            options=["Option A", "Option B", "Option C", "Option D"],
            answer_key=1,
            points=3,
        ),
        Question(
            prompt="Write a short essay explaining the implications.",
            type="essay",
            points=10,
        ),
    ]
    payload = AssessmentCreate(
        title=title,
        description=description,
        source_type=source_type,
        source_reference=filename,
        questions=fake_questions,
    )
    inserted_id = create_document("assessment", payload.dict())
    return Assessment(id=inserted_id, **payload.dict())


# -----------------------
# Submissions & Grading
# -----------------------

@app.post("/submissions", response_model=Dict[str, Any])
def create_submission(payload: SubmissionCreate):
    # Basic validation: ensure assessment exists and answers align
    from bson import ObjectId  # type: ignore
    assessment = db["assessment"].find_one({"_id": ObjectId(payload.assessment_id)})
    if not assessment:
        raise HTTPException(status_code=404, detail="Assessment not found")

    data = payload.dict()
    inserted_id = create_document("submission", data)
    return {"id": inserted_id, **data}


@app.post("/submissions/{submission_id}/grade", response_model=GradeResult)
def grade_submission(submission_id: str):
    from bson import ObjectId  # type: ignore
    sub = db["submission"].find_one({"_id": ObjectId(submission_id)})
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")

    assessment = db["assessment"].find_one({"_id": ObjectId(sub["assessment_id"])})
    if not assessment:
        raise HTTPException(status_code=404, detail="Assessment not found for submission")

    questions: List[Dict[str, Any]] = assessment.get("questions", [])
    answers: List[Dict[str, Any]] = sub.get("answers", [])

    total_points = sum(int(q.get("points", 1)) for q in questions)

    feedback: List[Dict[str, Any]] = []
    earned = 0
    for i, q in enumerate(questions):
        user_ans = next((a for a in answers if a.get("question_index") == i), None)
        correctness = 0.5  # default partial credit
        rationale = "Partial credit: baseline heuristic"

        if user_ans is not None:
            if q.get("type") == "multiple_choice":
                key = q.get("answer_key")
                if key is not None and user_ans.get("answer") == key:
                    correctness = 1.0
                    rationale = "Correct option"
                else:
                    correctness = 0.0
                    rationale = "Incorrect option"
            elif q.get("type") == "short_answer":
                # naive keyword match stub
                prompt_words = set(str(q.get("prompt", "")).lower().split())
                ans_words = set(str(user_ans.get("answer", "")).lower().split())
                overlap = len(prompt_words & ans_words)
                correctness = min(1.0, overlap / 5.0)
                rationale = f"Keyword overlap score: {overlap}"
            elif q.get("type") == "essay":
                # length-based stub
                length = len(str(user_ans.get("answer", "")))
                correctness = 1.0 if length > 200 else 0.6 if length > 80 else 0.3
                rationale = f"Length heuristic: {length} chars"
        pts = int(q.get("points", 1))
        earned += round(pts * correctness)
        feedback.append({
            "question_index": i,
            "points": pts,
            "earned": round(pts * correctness),
            "correctness": round(correctness, 2),
            "feedback": rationale,
        })

    score = round((earned / total_points) * 100.0, 2) if total_points > 0 else 0.0

    # Persist grading result to submission
    db["submission"].update_one(
        {"_id": ObjectId(submission_id)},
        {"$set": {"graded": True, "total_points": total_points, "score": score, "feedback": feedback}},
    )

    return GradeResult(graded=True, total_points=total_points, score=score, feedback=feedback)


# -----------------------
# Lessons
# -----------------------

@app.post("/lessons")
def create_lesson(payload: LessonCreate):
    inserted_id = create_document("lesson", payload.dict())
    return {"id": inserted_id, **payload.dict()}


@app.get("/lessons")
def list_lessons():
    docs = get_documents("lesson")
    return [serialize_doc(d) for d in docs]


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
