import re
import uuid
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from pydantic import BaseModel, Field, field_validator, model_validator
from app.models.loop import Genre, TempoFeel

TIME_SIGNATURE_RE = re.compile(r"[1-9]\d?/(1|2|4|8|16|32)")

# The denominators the format accepts, and the numerator ceiling, both mirror
# TIME_SIGNATURE_RE — a spelling this does not generate is one no loop can hold.
TIME_SIGNATURE_DENOMINATORS = (1, 2, 4, 8, 16, 32)
TIME_SIGNATURE_MAX_NUMERATOR = 99


def equivalent_time_signatures(signature: str) -> list[str]:
    """Every spelling of the same bar length, `signature` itself included.

    3/4 and 6/8 both fill a bar with six eighth notes, and the catalogue's
    producers write the two interchangeably, so free-text search treats them as
    one. Note this is *duration* equivalence, not a musical claim: 3/4 is a
    waltz and 6/8 a jig, and they differ in where the accent falls. The
    dedicated `time_signature` filter stays exact for callers who need that
    distinction.

    Rescaling to each accepted denominator — rather than a hand-kept table —
    means the set stays right for signatures nobody has thought about yet:
    3/4 yields 3/4, 6/8, 12/16 and 24/32, and drops 1.5/2 because half an
    eighth note is not a bar anyone can write down.
    """
    numerator, _, denominator = signature.partition("/")
    bar_length = Fraction(int(numerator), int(denominator))
    spellings = []
    for candidate_denominator in TIME_SIGNATURE_DENOMINATORS:
        scaled = bar_length * candidate_denominator
        if scaled.denominator != 1:
            continue
        if not 1 <= scaled.numerator <= TIME_SIGNATURE_MAX_NUMERATOR:
            continue
        spellings.append(f"{scaled.numerator}/{candidate_denominator}")
    return spellings

# The ceiling was 140, which pre-dates half the catalogue: drill sits around
# 140-145, seben and soukous run past 150, and anything counted in double time
# reads higher still. 250 covers those without letting a typo through.
# The floor was 60, which refused the slow end for the same reason: half-time
# trap counts near 60-70, ambient and downtempo beds sit below that, and a
# 6/8 groove counted in dotted beats reads lower still. 40 covers those.
BPM_MIN = 40
BPM_MAX = 250


class LoopCreate(BaseModel):
    title: str
    description: str | None = None
    genre: Genre
    bpm: int
    time_signature: str = "4/4"
    tempo_feel: TempoFeel
    tags: list[str] = []
    price: Decimal
    is_free: bool = False
    desired_price_usd: Decimal | None = Field(default=None, gt=0)

    @field_validator("bpm")
    @classmethod
    def bpm_range(cls, v: int) -> int:
        if not (BPM_MIN <= v <= BPM_MAX):
            raise ValueError(f"BPM must be between {BPM_MIN} and {BPM_MAX}")
        return v

    @field_validator("time_signature")
    @classmethod
    def time_signature_format(cls, v: str) -> str:
        value = v.strip()
        if not TIME_SIGNATURE_RE.fullmatch(value):
            raise ValueError(
                "Time signature must be in numerator/denominator format, e.g. 4/4"
            )
        return value


class LoopUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    genre: Genre | None = None
    bpm: int | None = None
    time_signature: str | None = None
    tempo_feel: TempoFeel | None = None
    tags: list[str] | None = None
    price: Decimal | None = None
    is_free: bool | None = None
    desired_price_usd: Decimal | None = Field(default=None, gt=0)

    @field_validator("bpm")
    @classmethod
    def bpm_range(cls, v: int | None) -> int | None:
        if v is not None and not (BPM_MIN <= v <= BPM_MAX):
            raise ValueError(f"BPM must be between {BPM_MIN} and {BPM_MAX}")
        return v

    @field_validator("time_signature")
    @classmethod
    def time_signature_format(cls, v: str | None) -> str | None:
        if v is None:
            return v
        value = v.strip()
        if not TIME_SIGNATURE_RE.fullmatch(value):
            raise ValueError(
                "Time signature must be in numerator/denominator format, e.g. 4/4"
            )
        return value


# Every value Loop.status takes: "processing" from upload until the encode
# job finishes, then "ready", or "failed" once the job has exhausted its
# retries. Named here so a filter cannot ask for a spelling no row can hold.
LOOP_STATUSES = ("ready", "processing", "failed")


class LoopResponse(BaseModel):
    id: uuid.UUID
    title: str
    slug: str
    genre: Genre
    bpm: int
    time_signature: str
    key: str | None = None
    duration: int
    tempo_feel: TempoFeel
    description: str | None = None
    tags: list[str]
    price: Decimal
    is_free: bool
    is_paid: bool
    store_product_id: str | None = None
    preview_s3_key: str | None = Field(default=None, exclude=True)
    thumbnail_s3_key: str | None = Field(default=None, exclude=True)
    preview_url: str | None = None
    thumbnail_url: str | None = None
    waveform_data: list | None
    download_count: int
    play_count: int
    created_at: datetime

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def build_urls(self) -> "LoopResponse":
        from app.config import get_settings
        base = get_settings().s3_cloudfront_url.rstrip("/")
        if self.preview_s3_key:
            self.preview_url = f"{base}/{self.preview_s3_key}" if base else self.preview_s3_key
        if self.thumbnail_s3_key:
            self.thumbnail_url = f"{base}/{self.thumbnail_s3_key}" if base else self.thumbnail_s3_key
        return self


class LoopAdminResponse(LoopResponse):
    """A loop as an administrator needs to see it.

    `status` is the whole point: a loop being re-encoded is not in the public
    catalogue, and without it in the payload an admin listing cannot tell a
    loop that is briefly reprocessing from one whose job failed hours ago.
    """

    status: str


class LoopFilter(BaseModel):
    # Public callers set this; producer and admin listings leave it off so a
    # producer can still watch their own uploads move through processing.
    ready_only: bool = False
    # Exact status, for an admin asking what is stuck rather than what is
    # browsable. `ready_only` is the public listing's blunter version.
    status: str | None = None
    search: str | None = None
    genre: Genre | None = None
    bpm_min: int | None = None
    bpm_max: int | None = None
    key: str | None = None
    # Many-valued, like tags: a compound-meter filter wants 6/8, 9/8 and 12/8
    # in one request. Accepts a comma-separated string, a repeated query
    # parameter, or a list built in code.
    time_signature: list[str] | None = None
    tempo_feel: TempoFeel | None = None
    tags: list[str] | None = None
    is_free: bool | None = None
    sort: str = "newest"
    page: int = 1
    page_size: int = 20
    created_by: uuid.UUID | None = None

    @field_validator("time_signature", mode="before")
    @classmethod
    def parse_time_signatures(cls, v) -> list[str] | None:
        """Flatten and validate however the caller spelled the filter.

        `6/8`, `6/8,9/8`, a repeated `?time_signature=` and a list built in
        code all arrive here and leave as a list of stored-form signatures.
        Malformed entries are rejected rather than dropped: silently ignoring
        one turns a client typo into a page of results for the *other* values,
        which reads as real data. Handlers turn the resulting ValidationError
        into a 422 (see parse_model).
        """
        if v is None:
            return None
        raw_values = v if isinstance(v, (list, tuple)) else [v]
        signatures: list[str] = []
        for raw in raw_values:
            for part in str(raw).split(","):
                signature = part.strip()
                if not signature:
                    continue
                if not TIME_SIGNATURE_RE.fullmatch(signature):
                    raise ValueError(
                        f"Time signature {signature!r} must be in "
                        "numerator/denominator format, e.g. 4/4"
                    )
                # Deduplicated so a repeated value cannot skew the IN list;
                # order is kept so the message above names them as sent.
                if signature not in signatures:
                    signatures.append(signature)
        # An all-whitespace value is not a filter at all — it must not turn
        # into an empty IN list, which matches no row.
        return signatures or None

    @field_validator("status")
    @classmethod
    def known_status(cls, v: str | None) -> str | None:
        if v is None:
            return v
        value = v.strip().lower()
        if value not in LOOP_STATUSES:
            raise ValueError(
                f"Status must be one of {', '.join(LOOP_STATUSES)}"
            )
        return value

    @field_validator("page_size")
    @classmethod
    def cap_page_size(cls, v: int) -> int:
        # One request must not be able to ask for the whole table. Capped
        # rather than rejected so an over-eager client gets a page instead of
        # an error it never handled before.
        return min(max(v, 1), 100)

    @field_validator("page")
    @classmethod
    def floor_page(cls, v: int) -> int:
        # page 0 becomes OFFSET -20, which Postgres refuses outright — a 500
        # for what is really a client typo.
        return max(v, 1)

