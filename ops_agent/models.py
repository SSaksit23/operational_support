"""Data model for everything the readers extract.

Every extracted record carries a SourceRef so a later step (verification,
message drafting, the review screen) can point back to the exact file and
cell/page/line a value came from.
"""

from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class DocType(str, Enum):
    NAME_LIST = "name_list"
    PREP_LETTER = "prep_letter"
    ETICKET = "eticket"
    PNR = "pnr"
    LAND_CONFIRMATION = "land_confirmation"
    EXPENSE_TEMPLATE = "expense_template"
    PASSPORT = "passport"
    UNKNOWN = "unknown"


class SourceRef(BaseModel):
    file: str
    doc_type: DocType
    location: str  # e.g. "Worksheet!D16", "page 3", "line 25"
    raw: Optional[str] = None  # original text before normalisation


class FlightLeg(BaseModel):
    flight_no: str  # "PN6430"
    date: Optional[dt.date] = None
    origin: Optional[str] = None  # IATA, "BKK"
    destination: Optional[str] = None
    dep_time: Optional[str] = None  # "19:35"
    arr_time: Optional[str] = None
    carrier_name: Optional[str] = None
    status: Optional[str] = None  # "OK", "RR24"
    source: SourceRef


class Passenger(BaseModel):
    no: Optional[int] = None
    room: Optional[str] = None  # "TRP01"
    title: Optional[str] = None
    given_name: str
    surname: str
    given_name_th: Optional[str] = None
    surname_th: Optional[str] = None
    gender: Optional[str] = None
    age: Optional[int] = None
    dob: Optional[dt.date] = None
    citizen_id: Optional[str] = None
    passport_no: Optional[str] = None
    passport_issue: Optional[dt.date] = None
    passport_expiry: Optional[dt.date] = None
    tel: Optional[str] = None
    agency: Optional[str] = None
    booking_no: Optional[str] = None
    remark: Optional[str] = None
    is_tour_leader: bool = False
    source: SourceRef

    @property
    def airline_name(self) -> str:
        """SURNAME/GIVEN, the form tickets and PNRs use."""
        return f"{self.surname}/{self.given_name}".upper()


class NameListDoc(BaseModel):
    tour_code: str
    program_name: Optional[str] = None
    program_code: Optional[str] = None  # "2UCKG-PN001"
    operator: Optional[str] = None  # "2UCENTER"
    tour_leader: Optional[str] = None
    pax_total: Optional[int] = None
    pax_breakdown: Optional[str] = None  # "12ADL+1TL"
    rooms_total: Optional[int] = None
    room_mix: dict[str, int] = Field(default_factory=dict)  # {"SGL": 1, "TRP": 2, ...}
    start_date: Optional[dt.date] = None
    end_date: Optional[dt.date] = None
    flights: list[FlightLeg] = Field(default_factory=list)
    passengers: list[Passenger] = Field(default_factory=list)
    source: SourceRef


class Ticket(BaseModel):
    passenger_name: str  # SURNAME/GIVEN
    ticket_no: str
    passport_no: Optional[str] = None
    airline_record: Optional[str] = None  # airline PNR, "MFWD4L"
    issuing_airline: Optional[str] = None
    issue_date: Optional[dt.date] = None
    legs: list[FlightLeg] = Field(default_factory=list)
    source: SourceRef


class ETicketDoc(BaseModel):
    tickets: list[Ticket] = Field(default_factory=list)
    source: SourceRef


class PNRPassenger(BaseModel):
    pnr_no: int  # passenger number inside the PNR, "P13"
    name: str  # SURNAME/GIVEN
    ticket_no: Optional[str] = None
    source: SourceRef


class PNRDoc(BaseModel):
    passengers: list[PNRPassenger] = Field(default_factory=list)
    segments: list[FlightLeg] = Field(default_factory=list)
    source: SourceRef


class Contact(BaseModel):
    role: str  # "tour_leader", "emergency", "guide", "land_operator"
    name: str
    nickname: Optional[str] = None
    phone: Optional[str] = None
    wechat: Optional[str] = None
    source: SourceRef


class HotelNight(BaseModel):
    date: dt.date
    hotel_name: str
    address: Optional[str] = None
    nights: int = 1
    phone: Optional[str] = None
    source: SourceRef


class OptionalTour(BaseModel):
    name: str
    price: Optional[float] = None
    currency: Optional[str] = None
    source: SourceRef


class Meeting(BaseModel):
    date: Optional[dt.date] = None
    time: Optional[str] = None
    airport: Optional[str] = None
    floor: Optional[str] = None
    gate: Optional[str] = None
    counter: Optional[str] = None
    sign: Optional[str] = None
    source: SourceRef


class PrepLetterDoc(BaseModel):
    program_title: Optional[str] = None
    duration: Optional[str] = None  # "5 วัน 4 คืน"
    airline: Optional[str] = None
    start_date: Optional[dt.date] = None
    end_date: Optional[dt.date] = None
    meeting: Optional[Meeting] = None
    contacts: list[Contact] = Field(default_factory=list)
    flights: list[FlightLeg] = Field(default_factory=list)
    hotels: list[HotelNight] = Field(default_factory=list)
    optional_tours: list[OptionalTour] = Field(default_factory=list)
    carry_on_kg: Optional[int] = None
    checked_kg: Optional[int] = None
    source: SourceRef


class Meals(BaseModel):
    date: dt.date
    breakfast: Optional[str] = None
    lunch: Optional[str] = None
    dinner: Optional[str] = None
    source: SourceRef


class LandConfirmationDoc(BaseModel):
    land_operator: Optional[Contact] = None
    guides: list[Contact] = Field(default_factory=list)
    other_contacts: list[Contact] = Field(default_factory=list)  # names and phones not labelled with a role
    hotels: list[HotelNight] = Field(default_factory=list)
    optional_tours: list[OptionalTour] = Field(default_factory=list)
    pax_total: Optional[int] = None
    adults: Optional[int] = None
    escorts: Optional[int] = None  # 陪 = tour leader(s) travelling with the group
    tour_code: Optional[str] = None  # the land operator's own group number (我社团号)
    agent_tour_code: Optional[str] = None  # our group number as they wrote it (贵社团号)
    start_date: Optional[dt.date] = None
    end_date: Optional[dt.date] = None
    tour_leader: Optional[str] = None  # 领队
    route: Optional[str] = None
    rooms: dict[str, int] = Field(default_factory=dict)  # {"标准间": 7, "大床房": 6}
    hotel_standard: Optional[str] = None
    meal_plan: Optional[str] = None  # "5早 11正"
    meals: list[Meals] = Field(default_factory=list)
    coach: Optional[str] = None
    flights: list[FlightLeg] = Field(default_factory=list)
    itinerary: list[str] = Field(default_factory=list)
    confirmed_on: Optional[dt.date] = None
    layout: str = "team_confirmation"  # "team_confirmation" | "general" | "llm"
    notes: list[SourceRef] = Field(default_factory=list)  # things the reader noticed, e.g. a blank guide field
    source: SourceRef


class Slot(BaseModel):
    name: str
    value: Optional[float | int | str] = None
    source: SourceRef


class ExpenseTemplateDoc(BaseModel):
    slots: dict[str, Slot] = Field(default_factory=dict)
    company_headers: dict[str, list[str]] = Field(default_factory=dict)  # sheet -> header lines
    images: list[Slot] = Field(default_factory=list)  # embedded pictures: value = sha256 of the image
    source: SourceRef


class PassportScan(BaseModel):
    """One passport's machine-readable zone. A field is None when its check digit failed."""
    surname: str
    given_name: str
    passport_no: Optional[str] = None
    nationality: Optional[str] = None
    dob: Optional[dt.date] = None
    sex: Optional[str] = None
    expiry: Optional[dt.date] = None
    checks_ok: bool = False
    source: SourceRef

    @property
    def airline_name(self) -> str:
        return f"{self.surname}/{self.given_name}"


class GroupBundle(BaseModel):
    """Everything read for one group, ready for the verification step."""

    name_list: Optional[NameListDoc] = None
    prep_letter: Optional[PrepLetterDoc] = None
    etickets: list[ETicketDoc] = Field(default_factory=list)
    pnrs: list[PNRDoc] = Field(default_factory=list)
    land_confirmation: Optional[LandConfirmationDoc] = None
    expense_template: Optional[ExpenseTemplateDoc] = None
    passports: list[PassportScan] = Field(default_factory=list)
    unread: list[SourceRef] = Field(default_factory=list)
