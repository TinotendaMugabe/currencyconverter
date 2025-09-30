"""
Unified AI-Powered Response Engine for National Tickets - Free-text Origin/Destination & Operator Selection

Changes in this version:
- Removed hardcoded city menus and city→geohash mapping
- Accept free-text for departure and destination; origin may be a geohash
- Removed static city route fallback; user selects operator from a flat catalog
- Proceed with booking regardless of operator coverage for the entered route
"""

import json
import time
import random
import asyncio
from typing import Dict, List, Optional, Any
import logging
import re
from datetime import datetime
from enum import Enum
import httpx

# Mock imports for services
try:
    from intent_service import detect_intent
except ImportError:
    def detect_intent(message, context=None):
        msg = message.lower()
        if any(word in msg for word in ["hi", "hello", "good morning", "hey"]):
            return {"intent": "greeting", "entities": {}, "confidence": 0.9}
        elif any(word in msg for word in ["book", "ticket", "bus", "travel"]):
            return {"intent": "book_bus", "entities": {}, "confidence": 0.8}
        elif any(word in msg for word in ["pay", "payment", "cost", "price"]):
            return {"intent": "payment_info", "entities": {}, "confidence": 0.8}
        elif any(word in msg for word in ["flight", "airline"]):
            return {"intent": "book_flight", "entities": {}, "confidence": 0.8}
        else:
            return {"intent": "general", "entities": {}, "confidence": 0.5}

try:
    from gemini_learning_service import learning_service
    from conversation_memory import conversation_memory, MessageRole
    from web_search_service import web_search_service
except ImportError:
    class MockService:
        def add_message(self, **kwargs): pass
        def mark_conversation_success(self, *args): return True
        def get_learning_statistics(self): return {}
        def search_national_tickets_info(self, *args): return {}
        def should_trigger_web_search(self, *args): return False
        def get_conversation_context(self, *args): return {}
        def mark_session_success(self, *args): return True
        def get_conversation_analytics(self): return {}
        def store_conversation(self, *args, **kwargs): pass
        def get_learning_context(self, *args): return {}
    learning_service = MockService()
    conversation_memory = MockService()
    web_search_service = MockService()
    MessageRole = type('MessageRole', (), {'USER': 'user', 'MELISSA': 'melissa'})()

logger = logging.getLogger(__name__)


class ConversationState(Enum):
    CONVERSATIONAL = "conversational"
    BOOKING_MENU = "booking_menu"
    BOOKING_COMPLETE = "booking_complete"
    AIRLINE_BOOKING = "airline_booking"
    HELPDESK = "helpdesk"


class BookingStep(Enum):
    MAIN_MENU = "main_menu"
    DEPARTURE_SELECTION = "departure_selection"
    DESTINATION_SELECTION = "destination_selection"
    OPERATOR_SELECTION = "operator_selection"
    PAYMENT_METHOD_SELECTION = "payment_method_selection"
    PAYMENT_INSTRUCTIONS = "payment_instructions"
    PAYMENT_PROOF_UPLOAD = "payment_proof_upload"
    PURCHASER_FIRST_NAME = "purchaser_first_name"
    PURCHASER_LAST_NAME = "purchaser_last_name"
    PURCHASER_EMAIL = "purchaser_email"
    PURCHASER_PHONE = "purchaser_phone"
    PURCHASER_CATEGORY = "purchaser_category"
    PURCHASER_ADDRESS1 = "purchaser_address1"
    PURCHASER_ADDRESS2 = "purchaser_address2"
    PURCHASER_CITY = "purchaser_city"
    PURCHASER_PROVINCE = "purchaser_province"
    PURCHASER_POSTCODE = "purchaser_postcode"
    PURCHASER_COUNTRY = "purchaser_country"
    TRAVEL_DATE = "travel_date"
    TRAVEL_TIME = "travel_time"
    NEXT_OF_KIN = "next_of_kin"
    BOOKING_SUMMARY = "booking_summary"
    BOOKING_CONFIRMED = "booking_confirmed"


KNOWLEDGE_SKELETON = {
    "company_info": {
        "name": "National Tickets Global",
        "agent_name": "Melissa",
        "location": "160A Old Enterprise Rd, Total Energies Service Station, Newlands",
        "services": ["bus bookings", "flight reservations", "route information"],
        "social": "https://www.facebook.com/natticks"
    },
    # Removed city route mappings. Keep a flat operator catalog for user selection.
    "operators": [
        "Eagleliner",
        "Citylink"
    ],
    "payment_methods": {
        "bank": {"name": "Cabs Avondale", "account": "1124821880", "swift": "Cabszwha"},
        "ecocash_usd": "*153*2*2*49926*Amount#",
        "innbucks": {"name": "Macmillan", "phone": "0778690679"}
    },
    "countries": {"ZW": "Zimbabwe", "ZA": "South Africa", "BW": "Botswana", "MZ": "Mozambique", "ZM": "Zambia"},
    "purchaser_categories": ["Adult", "Student", "Senior", "Child", "Infant"],
    "policies": {
        "cancellation": "Cancellation, refund or change will attract a charge",
        "missed_bus": "If you miss the bus there will be no refund or transfer of ticket",
        "check_in": "Please bring your ID at check-in to board the bus",
        "unaccompanied_minors": "Unaccompanied Children (Under 13 yrs) are not permitted on Eagleliner Coaches",
        "flight_checkin": "Check in at least 3 hours before departure time",
        "baggage": "Carry-on should not exceed 7kg, one personal item allowed"
    },
    "faq": {
        "what_is_national_tickets": "National Tickets Global is Zimbabwe's premier travel booking service, offering bus bookings, flight reservations, and comprehensive travel support.",
        "office_location": "Our office is located at 160A Old Enterprise Rd, Total Energies Service Station, Newlands.",
        "contact_methods": "You can contact us through this chat, visit our Facebook page at https://www.facebook.com/natticks, or visit our office.",
        "booking_requirements": "For booking, you'll need: Name, Email, Phone number, address details, travel date, departure/destination cities, and next of kin details."
    }
}


class BusbudAPIClient:
    def __init__(self, middleware_base_url: str = None):
        self.base_url = middleware_base_url or "http://localhost:3002"
        self.client = httpx.AsyncClient(timeout=30.0)

    async def __aenter__(self): return self
    async def __aexit__(self, exc_type, exc_val, exc_tb): await self.client.aclose()

    async def _make_request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        try:
            logger.info(f"Making {method} request to {url}")
            resp = await self.client.request(method, url, **kwargs)
            resp.raise_for_status()
            if resp.headers.get('content-type', '').startswith('application/json'):
                return resp.json()
            return {"success": True, "data": resp.text}
        except httpx.HTTPStatusError as e:
            try:
                data = e.response.json()
            except Exception:
                data = {"error": e.response.text}
            raise Exception(f"API Error {e.response.status_code}: {data.get('message', 'Unknown error')}")
        except Exception as e:
            raise Exception(f"Connection failed: {str(e)}")

    async def search_routes(self, origin_geohash: str, destination_geohash: str,
                            travel_date: str, adults: int = 1) -> Dict[str, Any]:
        endpoint = "/api/search/routes"
        params = {"origin": origin_geohash, "destination": destination_geohash, "date": travel_date, "adults": adults}
        try:
            result = await self._make_request("GET", endpoint, params=params)
            return {
                "success": result.get("success", True),
                "trips": result.get("trips", []),
                "search_id": result.get("searchId"),
                "metadata": {
                    "pollUrl": result.get("pollUrl"),
                    "cities": result.get("cities", {}),
                    "message": result.get("message", "")
                }
            }
        except Exception as e:
            logger.error(f"Route search failed: {e}")
            return {"success": False, "error": str(e), "trips": []}


class UnifiedBookingEngine:
    def __init__(self, gemini_client=None):
        self.gemini_client = gemini_client
        self.conversation_context = {}
        self.learning_service = learning_service
        self.conversation_memory = conversation_memory
        self.web_search_service = web_search_service
        self.booking_counter = 1000
        self.busbud_client = None
        # Removed city geohash mapping and static routes fallback
        self.response_analytics = {
            "total_responses": 0,
            "conversational_responses": 0,
            "menu_driven_responses": 0,
            "state_transitions": 0,
            "api_calls": 0,
            "successful_bookings": 0
        }
        logger.info("Unified Booking Engine initialized (free-text origin/destination)")

    async def _get_busbud_client(self) -> BusbudAPIClient:
        if self.busbud_client is None:
            self.busbud_client = BusbudAPIClient()
        return self.busbud_client

    # Geohash is optional; origin input may be a geohash. No static fallbacks are used.
    GE0HASH_REGEX = re.compile(r"^[0-9bcdefghjkmnpqrstuvwxyz]{5,9}$")

    def _is_geohash(self, text: str) -> bool:
        return bool(self.GE0HASH_REGEX.match(text.strip().lower()))

    def _initialize_session(self, session_id: str):
        if session_id not in self.conversation_context:
            self.conversation_context[session_id] = {
                "conversation_state": ConversationState.CONVERSATIONAL,
                "booking_step": None,
                "booking_data": {},
                "session_id": session_id,
                "last_intent": None,
                "context_data": {},
                "first_greeting_sent": False,
                "message_count": 0
            }
            logger.info(f"Initialized new session: {session_id}")

    def _is_in_booking_flow(self, session_context: Dict) -> bool:
        if session_context.get("conversation_state") == ConversationState.BOOKING_MENU:
            return True
        return bool(session_context.get("booking_data"))

    def _should_use_menu_mode(self, intent: str, entities: Dict, message: str, session_context: Dict) -> bool:
        msg = message.lower().strip()
        if msg in ["1", "bus ticketing"]:
            return True
        if any(p in msg for p in ["book a ticket", "book ticket", "book bus", "start booking", "make a booking", "i want to book", "book a bus", "reserve a ticket"]):
            return True
        if intent in ["book_bus", "book_ticket", "start_booking", "make_reservation"]:
            return True
        if not self._is_in_booking_flow(session_context) and any(k in msg for k in ["my name is", "passenger name", "travel date is"]):
            return True
        if any(k in msg for k in ["what", "how", "why", "tell me", "explain", "about", "services", "company", "information", "details", "more about", "describe"]):
            return False
        if intent in ["policy", "faq", "general", "greeting", "payment_info"]:
            return False
        return False

    def _should_stay_conversational(self, intent: str, message: str, session_context: Dict) -> bool:
        msg = message.lower().strip()
        if self._is_in_booking_flow(session_context):
            if any(k in msg for k in ["what is", "how do", "why", "explain", "tell me about", "what are", "how can", "describe", "information about"]):
                return True
            return False
        if intent in ["greeting", "general", "faq", "policy", "help", "payment_info"]:
            return True
        if any(k in msg for k in ["what", "how", "why", "explain", "tell me", "about", "describe", "information", "details", "services", "company", "where", "when"]):
            return True
        if session_context.get("conversation_state") == ConversationState.BOOKING_COMPLETE:
            return True
        if any(k in msg for k in ["policy", "rule", "cancel", "refund", "help", "support", "contact"]):
            return True
        return False

    def _get_conversational_context(self, intent: str, entities: Dict, user_message: str, session_id: str) -> Dict:
        context = {
            "company": KNOWLEDGE_SKELETON["company_info"],
            "relevant_data": {},
            "conversation_context": {},
            "learning_context": {},
            "session_id": session_id
        }
        if intent in ["payment_info"] or "pay" in user_message.lower():
            context["relevant_data"]["payments"] = KNOWLEDGE_SKELETON["payment_methods"]
        if intent in ["policy", "faq"] or any(w in user_message.lower() for w in ["policy", "rule", "cancel", "refund"]):
            context["relevant_data"]["policies"] = KNOWLEDGE_SKELETON["policies"]
            context["relevant_data"]["faq"] = KNOWLEDGE_SKELETON["faq"]
        try:
            context["conversation_context"] = self.conversation_memory.get_conversation_context(session_id)
        except Exception as e:
            logger.error(f"Error getting conversation context: {e}")
        try:
            context["learning_context"] = self.learning_service.get_learning_context(user_message, intent, entities)
        except Exception as e:
            logger.error(f"Error getting learning context: {e}")
        return context

    def _generate_conversational_response(self, user_message: str, session_id: str, intent: str, entities: Dict) -> Dict:
        context = self._get_conversational_context(intent, entities, user_message, session_id)
        web_results = None
        try:
            if self.web_search_service.should_trigger_web_search(user_message, intent, 0.5):
                web_results = self.web_search_service.search_national_tickets_info(user_message, context.get("conversation_context", {}).get("current_stage", ""))
        except Exception as e:
            logger.error(f"Web search error: {e}")
        try:
            if self.gemini_client:
                prompt = self._build_conversational_prompt(user_message, intent, entities, context, web_results)
                resp = self.gemini_client.generate_content(prompt)
                ai_text = resp.text if hasattr(resp, 'text') else str(resp)
            else:
                ai_text = self._generate_template_conversational_response(intent, entities, context, user_message)
        except Exception as e:
            logger.error(f"Conversational AI error: {e}")
            ai_text = self._generate_template_conversational_response(intent, entities, context, user_message)
        return {"response": ai_text, "mode": "conversational", "intent": intent, "entities": entities, "web_search_used": bool(web_results)}

    def _generate_template_conversational_response(self, intent: str, entities: Dict, context: Dict, user_message: str) -> str:
        msg = user_message.lower()
        session_id = context.get('session_id', '')
        sess = self.conversation_context.get(session_id, {})
        first = not sess.get("first_greeting_sent", False)
        if intent == "greeting" or any(w in msg for w in ["hi", "hello", "good morning"]):
            if first:
                if session_id in self.conversation_context:
                    self.conversation_context[session_id]["first_greeting_sent"] = True
                return ("Thank you for contacting National Tickets Global, my name is Melissa, and I will be your support agent today. "
                        "I can help you with bus bookings, flight reservations, route information, and answer any questions you have. "
                        "How may I assist you?")
            return random.choice([
                "Hello again! How can I help you today?",
                "Hi there! What can I assist you with?",
                "Welcome back! How may I help you?",
                "Good to hear from you! What do you need assistance with?",
                "Hi! I'm here to help. What can I do for you?",
                "Hello! How can I assist you with your travel needs?"
            ])
        return random.choice([
            "I'm Melissa, your support agent at National Tickets Global. I can help you with bus bookings, flight reservations, route information, payment methods, and policies. What would you like assistance with today?",
            "Hi! I'm here to help with all your travel needs - bus tickets, flights, routes, and more. How can I assist you?",
            "I'm ready to help with your travel plans! Whether it's booking tickets, route info, or answering questions - just let me know what you need."
        ])

    def _is_valid_email(self, email: str) -> bool:
        return bool(re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email))

    def _is_valid_date(self, date_str: str) -> bool:
        if not re.match(r'^\d{2}/\d{2}/\d{4}$', date_str):
            return False
        try:
            day, month, year = map(int, date_str.split('/'))
            return 1 <= day <= 31 and 1 <= month <= 12 and year >= 2024
        except:
            return False

    def _generate_purchaser_summary(self, purchaser: Dict, current_step: BookingStep) -> str:
        steps = [
            (BookingStep.PURCHASER_FIRST_NAME, "First Name"),
            (BookingStep.PURCHASER_LAST_NAME, "Last Name"),
            (BookingStep.PURCHASER_EMAIL, "Email"),
            (BookingStep.PURCHASER_PHONE, "Phone"),
            (BookingStep.PURCHASER_CATEGORY, "Category"),
            (BookingStep.PURCHASER_ADDRESS1, "Address Line 1"),
            (BookingStep.PURCHASER_ADDRESS2, "Address Line 2"),
            (BookingStep.PURCHASER_CITY, "City"),
            (BookingStep.PURCHASER_PROVINCE, "Province"),
            (BookingStep.PURCHASER_POSTCODE, "Postal Code"),
            (BookingStep.PURCHASER_COUNTRY, "Country")
        ]
        completed, pending = [], []
        for step, field_name in steps:
            key = field_name.lower().replace(" ", "_")
            val = purchaser.get(key, "").strip()
            if val: completed.append(f"✅ {field_name}: {val}")
            else: pending.append(f"⏳ {field_name}: [Not provided yet]")
        lines = completed + pending
        for i, (step, field_name) in enumerate(steps):
            if step == current_step:
                lines[i] = lines[i].replace("✅", "➡️").replace("⏳", "➡️")
                break
        return "\n".join(lines)

    def _navigate_purchaser_back(self, current_step: BookingStep, session_context: Dict) -> Dict:
        order = [
            BookingStep.PURCHASER_FIRST_NAME, BookingStep.PURCHASER_LAST_NAME, BookingStep.PURCHASER_EMAIL,
            BookingStep.PURCHASER_PHONE, BookingStep.PURCHASER_CATEGORY, BookingStep.PURCHASER_ADDRESS1,
            BookingStep.PURCHASER_ADDRESS2, BookingStep.PURCHASER_CITY, BookingStep.PURCHASER_PROVINCE,
            BookingStep.PURCHASER_POSTCODE, BookingStep.PURCHASER_COUNTRY, BookingStep.TRAVEL_DATE,
            BookingStep.TRAVEL_TIME, BookingStep.NEXT_OF_KIN, BookingStep.BOOKING_SUMMARY
        ]
        idx = order.index(current_step) if current_step in order else -1
        if idx > 0:
            prev = order[idx - 1]
            if prev == BookingStep.PURCHASER_FIRST_NAME: response = "Let's go back to first name. What is your first name?"
            elif prev == BookingStep.PURCHASER_LAST_NAME: response = "Let's go back to last name. What is your last name?"
            elif prev == BookingStep.PURCHASER_EMAIL: response = "Let's go back to email. What is your email address?"
            elif prev == BookingStep.PURCHASER_PHONE: response = "Let's go back to phone number. What is your phone number?"
            elif prev == BookingStep.PURCHASER_CATEGORY:
                cats = KNOWLEDGE_SKELETON["purchaser_categories"]
                response = f"Let's go back to category selection:\n\n{chr(10).join([f'{i+1} {c}' for i, c in enumerate(cats)])}"
            elif prev == BookingStep.PURCHASER_ADDRESS1: response = "Let's go back to address. What is your street address (Address Line 1)?"
            elif prev == BookingStep.PURCHASER_ADDRESS2: response = "Let's go back to address line 2. Address Line 2 (Optional - type 'skip' to skip):"
            elif prev == BookingStep.PURCHASER_CITY: response = "Let's go back to city. What is your city?"
            elif prev == BookingStep.PURCHASER_PROVINCE: response = "Let's go back to province. What is your province/state?"
            elif prev == BookingStep.PURCHASER_POSTCODE: response = "Let's go back to postal code. What is your postal/zip code?"
            elif prev == BookingStep.PURCHASER_COUNTRY:
                countries = KNOWLEDGE_SKELETON["countries"]
                response = f"Let's go back to country selection:\n\n{chr(10).join([f'{i+1} {name} ({code})' for i, (code, name) in enumerate(countries.items())])}"
            elif prev == BookingStep.TRAVEL_DATE: response = "Let's go back to travel date. What is your preferred travel date? (DD/MM/YYYY)"
            elif prev == BookingStep.TRAVEL_TIME: response = "Let's go back to travel time. What is your preferred travel time?"
            elif prev == BookingStep.NEXT_OF_KIN: response = "Let's go back to next of kin. What is your next of kin phone number?"
            else: response = "Let's go back to the previous step."
            return {"response": response, "new_step": prev, "mode": "menu"}
        return self._handle_main_menu("0", session_context)

    def _validate_purchaser_details(self, purchaser: Dict) -> Dict:
        required = {
            "first_name": BookingStep.PURCHASER_FIRST_NAME,
            "last_name": BookingStep.PURCHASER_LAST_NAME,
            "email": BookingStep.PURCHASER_EMAIL,
            "phone": BookingStep.PURCHASER_PHONE,
            "category": BookingStep.PURCHASER_CATEGORY,
            "address1": BookingStep.PURCHASER_ADDRESS1,
            "city": BookingStep.PURCHASER_CITY,
            "province": BookingStep.PURCHASER_PROVINCE,
            "postcode": BookingStep.PURCHASER_POSTCODE,
            "country_code": BookingStep.PURCHASER_COUNTRY
        }
        missing = []
        for field, step in required.items():
            if not purchaser.get(field, "").strip():
                missing.append((field.replace("_", " ").title(), step))
        if missing:
            msg = "\n".join([f"• {f}" for f, _ in missing])
            return {"valid": False, "message": f"The following information is required:\n\n{msg}", "missing_step": missing[0][1]}
        return {"valid": True, "message": "All details complete"}

    def _generate_booking_summary(self, booking: Dict) -> str:
        purchaser = booking.get("purchaser", {})
        return f"""BOOKING SUMMARY:

Route: {booking.get('departure_city', booking.get('departure_input', 'N/A'))} → {booking.get('destination_city', booking.get('destination_input', 'N/A'))}
Operator: {booking.get('operator', 'N/A')}
Fare: {booking.get('fare', 'N/A')}

Purchaser Details:
Name: {purchaser.get('first_name', 'N/A')} {purchaser.get('last_name', 'N/A')}
Email: {purchaser.get('email', 'N/A')}
Phone: {purchaser.get('phone', 'N/A')}
Category: {purchaser.get('category', 'N/A')}
Address: {purchaser.get('address1', 'N/A')}, {purchaser.get('city', 'N/A')}, {purchaser.get('province', 'N/A')} {purchaser.get('postcode', 'N/A')}
Country: {KNOWLEDGE_SKELETON['countries'].get(purchaser.get('country_code', 'ZW'), 'Zimbabwe')}

Travel Details:
Travel Date: {booking.get('travel_date', 'N/A')}
Travel Time: {booking.get('travel_time', 'N/A')}
Next of Kin: {booking.get('next_of_kin', 'N/A')}

Payment Method: {booking.get('payment_method', 'N/A').replace('_', ' ').title()}
Payment Proof: {booking.get('payment_proof', 'Offline Payment')}"""

    async def _handle_booking_confirmation_enhanced(self, message: str, session_context: Dict) -> Dict:
        msg = message.lower().strip()
        if msg in ["back", "previous", "b"]:
            return self._navigate_purchaser_back(BookingStep.BOOKING_SUMMARY, session_context)
        if msg in ["details", "show details", "my details"]:
            purchaser = session_context["booking_data"].get("purchaser", {})
            summary = self._generate_purchaser_summary(purchaser, BookingStep.BOOKING_SUMMARY)
            resp = f"""📋 YOUR PURCHASER DETAILS:

{summary}

Please type 'YES' to confirm booking or 'back' to modify details."""
            return {"response": resp, "new_step": BookingStep.BOOKING_SUMMARY, "mode": "menu"}
        if msg in ["yes", "y", "confirm", "ok", "proceed"]:
            booking = session_context["booking_data"]
            try:
                validation = self._validate_purchaser_details(booking.get("purchaser", {}))
                if not validation["valid"]:
                    resp = f"""❌ MISSING REQUIRED INFORMATION:

{validation['message']}

Please provide the missing information. Type 'back' to go back and complete your details."""
                    return {"response": resp, "new_step": validation["missing_step"], "mode": "menu"}

                # No real API booking in this simplified flow
                ticket = self._generate_ticket(booking, session_context["session_id"])
                self.response_analytics["successful_bookings"] += 1
                purchaser = booking.get("purchaser", {})
                resp = f"""✅ BOOKING CONFIRMED!

🎫 TICKET ISSUED
Ticket Number: {ticket['ticket_number']}
Reference: {ticket['reference_code']}

📋 BOOKING SUMMARY:
Route: {booking.get('departure_city', booking.get('departure_input', 'N/A'))} → {booking.get('destination_city', booking.get('destination_input', 'N/A'))}
Operator: {booking.get('operator', 'N/A')}
Date: {booking.get('travel_date', 'N/A')}
Time: {booking.get('travel_time', 'N/A')}
Fare: {booking.get('fare', 'Price on request')}

📧 A confirmation has been sent to: {purchaser.get('email', 'your email')}

Thank you for choosing National Tickets Global! Safe travels!

────────────────────
Need another booking? Send 'Hi' to start over.
Or ask me any questions about our services."""
                # reset
                session_context["booking_data"] = {}
                session_context["conversation_state"] = ConversationState.CONVERSATIONAL
                session_context["booking_step"] = None
                session_context["first_greeting_sent"] = False
                return {
                    "response": resp,
                    "new_step": None,
                    "new_state": ConversationState.CONVERSATIONAL,
                    "mode": "booking_complete",
                    "booking_complete": True
                }
            except Exception as e:
                logger.error(f"Booking confirmation error: {e}")
                return {"response": f"""❌ BOOKING ERROR

An unexpected error occurred while processing your booking: {str(e)}

Please try again later or contact our support team.

────────────────────
Send 'Hi' to start over.""", "new_step": BookingStep.BOOKING_SUMMARY, "mode": "menu"}
        return {"response": """Please review your booking details and type 'YES' to confirm.

Available commands:
• 'YES' - Confirm and complete booking
• 'details' - Show your purchaser details
• 'back' - Go back to modify information

Or type '0' to cancel and return to main menu.""", "new_step": BookingStep.BOOKING_SUMMARY, "mode": "menu"}

    def _format_operator_list(self) -> str:
        ops = KNOWLEDGE_SKELETON.get("operators", [])
        return "\n".join([f"{i+1}. {op}" for i, op in enumerate(ops)]) if ops else "No operators available"

    def _handle_payment_method_selection(self, message: str, session_context: Dict) -> Dict:
        numeric = self._is_numeric_selection(message)
        if numeric == 0:
            return self._handle_main_menu(message, session_context)
        methods = {1: "bank_transfer", 2: "ecocash_usd", 3: "innbucks", 4: "offline_payment"}
        if numeric in methods:
            sel = methods[numeric]
            session_context["booking_data"]["payment_method"] = sel
            if sel == "offline_payment":
                resp = """Payment Method: Offline Payment Selected

Now I need to collect your purchaser details.

What is your first name?
💡 Tip: Type 'details' at any time to see your entered information"""
                return {"response": resp, "new_step": BookingStep.PURCHASER_FIRST_NAME, "mode": "menu"}
            return self._show_payment_instructions(sel, session_context)
        resp = """Invalid selection. Please select your preferred payment method:

1. Bank Transfer
2. EcoCash USD
3. InnBucks
4. Complete Booking (Offline Payment)

QUICK LINKS
0 Back to Main Menu"""
        return {"response": resp, "new_step": BookingStep.PAYMENT_METHOD_SELECTION, "mode": "menu"}

    def _show_payment_instructions(self, payment_method: str, session_context: Dict) -> Dict:
        p = KNOWLEDGE_SKELETON["payment_methods"]
        if payment_method == "bank_transfer":
            resp = f"""BANK TRANSFER DETAILS:

Bank: {p['bank']['name']}
Account Number: {p['bank']['account']}
Swift Code: {p['bank']['swift']}

Please make your payment and then type 'DONE' when completed."""
        elif payment_method == "ecocash_usd":
            resp = f"""ECOCASH USD PAYMENT:

Dial: {p['ecocash_usd']}
(Replace 'Amount' with your fare amount)

Please make your payment and then type 'DONE' when completed."""
        elif payment_method == "innbucks":
            resp = f"""INNBUCKS PAYMENT:

Send payment to:
Name: {p['innbucks']['name']}
Phone: {p['innbucks']['phone']}

Please make your payment and then type 'DONE' when completed."""
        else:
            return {"response": "Payment method not recognized. Please try again.", "new_step": BookingStep.PAYMENT_METHOD_SELECTION, "mode": "menu"}
        return {"response": resp, "new_step": BookingStep.PAYMENT_INSTRUCTIONS, "mode": "menu"}

    def _handle_payment_instructions(self, message: str, session_context: Dict) -> Dict:
        if message.upper().strip() == "DONE":
            return {"response": "Payment completed! \n\nPlease provide your payment reference number or proof of payment:", "new_step": BookingStep.PAYMENT_PROOF_UPLOAD, "mode": "menu"}
        return {"response": "Please complete your payment and type 'DONE' when finished.\n\nQUICK LINKS\n0 Back to Main Menu", "new_step": BookingStep.PAYMENT_INSTRUCTIONS, "mode": "menu"}

    def _handle_payment_proof_upload(self, message: str, session_context: Dict) -> Dict:
        if message.strip():
            session_context["booking_data"]["payment_proof"] = message.strip()
            resp = """Payment proof received! 

Now I need to collect your purchaser details.

What is your first name?
💡 Tip: Type 'details' at any time to see your entered information"""
            return {"response": resp, "new_step": BookingStep.PURCHASER_FIRST_NAME, "mode": "menu"}
        return {"response": "Please provide your payment reference number or proof of payment:\n\nQUICK LINKS\n0 Back to Main Menu", "new_step": BookingStep.PAYMENT_PROOF_UPLOAD, "mode": "menu"}

    def _handle_purchaser_details(self, message: str, session_context: Dict, current_step: BookingStep) -> Dict:
        if self._is_numeric_selection(message) == 0:
            return self._handle_main_menu(message, session_context)
        booking = session_context["booking_data"]
        if "purchaser" not in booking:
            booking["purchaser"] = {"first_name": "", "last_name": "", "email": "", "phone": "", "category": "", "address1": "", "address2": "", "city": "", "province": "", "postcode": "", "country_code": ""}
        purchaser = booking["purchaser"]

        if message.lower().strip() in ["back", "previous", "b"]:
            return self._navigate_purchaser_back(current_step, session_context)

        if message.lower().strip() in ["details", "show details", "my details", "review"]:
            summary = self._generate_purchaser_summary(purchaser, current_step)
            resp = f"""📋 YOUR DETAILS SO FAR:

{summary}

Please continue with the current question or type 'back' to go to previous step."""
            return {"response": resp, "new_step": current_step, "mode": "menu"}

        if current_step == BookingStep.PURCHASER_FIRST_NAME:
            if not message.strip(): return {"response": "Please provide your first name:", "new_step": current_step, "mode": "menu"}
            purchaser["first_name"] = message.strip()
            return {"response": "What is your last name?\n\n💡 Tip: Type 'details' to see your entered information", "new_step": BookingStep.PURCHASER_LAST_NAME, "mode": "menu"}

        if current_step == BookingStep.PURCHASER_LAST_NAME:
            if not message.strip(): return {"response": "Please provide your last name:", "new_step": current_step, "mode": "menu"}
            purchaser["last_name"] = message.strip()
            return {"response": "What is your email address?\n\n💡 Tip: Type 'details' to see your entered information", "new_step": BookingStep.PURCHASER_EMAIL, "mode": "menu"}

        if current_step == BookingStep.PURCHASER_EMAIL:
            if not self._is_valid_email(message.strip()): return {"response": "Please provide a valid email address:", "new_step": current_step, "mode": "menu"}
            purchaser["email"] = message.strip()
            return {"response": "What is your phone number?\n\n💡 Tip: Type 'details' to see your entered information", "new_step": BookingStep.PURCHASER_PHONE, "mode": "menu"}

        if current_step == BookingStep.PURCHASER_PHONE:
            if not message.strip(): return {"response": "Please provide your phone number:", "new_step": current_step, "mode": "menu"}
            purchaser["phone"] = message.strip()
            cats = KNOWLEDGE_SKELETON["purchaser_categories"]
            return {"response": f"""Please select your category:

{chr(10).join([f"{i+1} {c}" for i, c in enumerate(cats)])}

💡 Tip: Type 'details' to see your entered information""", "new_step": BookingStep.PURCHASER_CATEGORY, "mode": "menu"}

        if current_step == BookingStep.PURCHASER_CATEGORY:
            cats = KNOWLEDGE_SKELETON["purchaser_categories"]
            sel = self._is_numeric_selection(message)
            if sel and 1 <= sel <= len(cats):
                purchaser["category"] = cats[sel - 1]
                return {"response": "What is your street address (Address Line 1)?\n\n💡 Tip: Type 'details' to see your entered information", "new_step": BookingStep.PURCHASER_ADDRESS1, "mode": "menu"}
            return {"response": f"""Invalid selection. Please select your category:

{chr(10).join([f"{i+1} {c}" for i, c in enumerate(cats)])}

💡 Tip: Type 'details' to see your entered information""", "new_step": BookingStep.PURCHASER_CATEGORY, "mode": "menu"}

        if current_step == BookingStep.PURCHASER_ADDRESS1:
            if not message.strip(): return {"response": "Please provide your street address:", "new_step": current_step, "mode": "menu"}
            purchaser["address1"] = message.strip()
            return {"response": "Address Line 2 (Optional - type 'skip' to skip or provide additional address details):\n\n💡 Tip: Type 'details' to see your entered information", "new_step": BookingStep.PURCHASER_ADDRESS2, "mode": "menu"}

        if current_step == BookingStep.PURCHASER_ADDRESS2:
            purchaser["address2"] = "" if message.lower().strip() == "skip" else message.strip()
            return {"response": "What is your city?\n\n💡 Tip: Type 'details' to see your entered information", "new_step": BookingStep.PURCHASER_CITY, "mode": "menu"}

        if current_step == BookingStep.PURCHASER_CITY:
            if not message.strip(): return {"response": "Please provide your city:", "new_step": current_step, "mode": "menu"}
            purchaser["city"] = message.strip()
            return {"response": "What is your province/state?\n\n💡 Tip: Type 'details' to see your entered information", "new_step": BookingStep.PURCHASER_PROVINCE, "mode": "menu"}

        if current_step == BookingStep.PURCHASER_PROVINCE:
            if not message.strip(): return {"response": "Please provide your province/state:", "new_step": current_step, "mode": "menu"}
            purchaser["province"] = message.strip()
            return {"response": "What is your postal/zip code?\n\n💡 Tip: Type 'details' to see your entered information", "new_step": BookingStep.PURCHASER_POSTCODE, "mode": "menu"}

        if current_step == BookingStep.PURCHASER_POSTCODE:
            if not message.strip(): return {"response": "Please provide your postal/zip code:", "new_step": current_step, "mode": "menu"}
            purchaser["postcode"] = message.strip()
            countries = KNOWLEDGE_SKELETON["countries"]
            return {"response": f"""Please select your country:

{chr(10).join([f"{i+1} {name} ({code})" for i, (code, name) in enumerate(countries.items())])}

💡 Tip: Type 'details' to see your entered information""", "new_step": BookingStep.PURCHASER_COUNTRY, "mode": "menu"}

        if current_step == BookingStep.PURCHASER_COUNTRY:
            codes = list(KNOWLEDGE_SKELETON["countries"].keys())
            sel = self._is_numeric_selection(message)
            if sel and 1 <= sel <= len(codes):
                purchaser["country_code"] = codes[sel - 1]
                summary = self._generate_purchaser_summary(purchaser, current_step)
                resp = f"""✅ Purchaser details captured so far:

{summary}

Now I need some travel details.

What is your preferred travel date? (DD/MM/YYYY)
💡 Tip: Type 'details' to see all your entered information"""
                return {"response": resp, "new_step": BookingStep.TRAVEL_DATE, "mode": "menu"}
            countries = KNOWLEDGE_SKELETON["countries"]
            return {"response": f"""Invalid selection. Please select your country:

{chr(10).join([f"{i+1} {name} ({code})" for i, (code, name) in enumerate(countries.items())])}

💡 Tip: Type 'details' to see your entered information""", "new_step": BookingStep.PURCHASER_COUNTRY, "mode": "menu"}

        if current_step == BookingStep.TRAVEL_DATE:
            if not self._is_valid_date(message.strip()):
                return {"response": "Please provide a valid travel date (DD/MM/YYYY):", "new_step": current_step, "mode": "menu"}
            booking["travel_date"] = message.strip()
            return {"response": "What is your preferred travel time? (e.g., 14:30 or 2:30 PM)\n\n💡 Tip: Type 'details' to see your entered information", "new_step": BookingStep.TRAVEL_TIME, "mode": "menu"}

        if current_step == BookingStep.TRAVEL_TIME:
            if not message.strip():
                return {"response": "Please provide your preferred travel time:", "new_step": current_step, "mode": "menu"}
            booking["travel_time"] = message.strip()
            return {"response": "What is your next of kin phone number?\n\n💡 Tip: Type 'details' to see your entered information", "new_step": BookingStep.NEXT_OF_KIN, "mode": "menu"}

        if current_step == BookingStep.NEXT_OF_KIN:
            if not message.strip():
                return {"response": "Please provide your next of kin phone number:", "new_step": current_step, "mode": "menu"}
            booking["next_of_kin"] = message.strip()
            summary = self._generate_booking_summary(booking)
            resp = f"""📋 BOOKING SUMMARY - FINAL REVIEW:

{summary}

Please review ALL your booking details above. 

Type 'YES' to confirm and complete booking, or type 'details' to see purchaser details only.

You can also type 'back' to go back and modify any information."""
            return {"response": resp, "new_step": BookingStep.BOOKING_SUMMARY, "mode": "menu"}

        return {"response": "Invalid step. Let's start over.", "new_step": BookingStep.MAIN_MENU, "mode": "menu"}

    async def _handle_menu_driven_flow(self, user_message: str, session_context: Dict) -> Dict:
        step = session_context.get("booking_step", BookingStep.MAIN_MENU)
        if step == BookingStep.MAIN_MENU: return self._handle_main_menu(user_message, session_context)
        if step == BookingStep.DEPARTURE_SELECTION: return self._handle_departure_free_text(user_message, session_context)
        if step == BookingStep.DESTINATION_SELECTION: return self._handle_destination_free_text(user_message, session_context)
        if step == BookingStep.OPERATOR_SELECTION: return self._handle_operator_selection(user_message, session_context)
        if step == BookingStep.PAYMENT_METHOD_SELECTION: return self._handle_payment_method_selection(user_message, session_context)
        if step == BookingStep.PAYMENT_INSTRUCTIONS: return self._handle_payment_instructions(user_message, session_context)
        if step == BookingStep.PAYMENT_PROOF_UPLOAD: return self._handle_payment_proof_upload(user_message, session_context)
        if step in [
            BookingStep.PURCHASER_FIRST_NAME, BookingStep.PURCHASER_LAST_NAME, BookingStep.PURCHASER_EMAIL,
            BookingStep.PURCHASER_PHONE, BookingStep.PURCHASER_CATEGORY, BookingStep.PURCHASER_ADDRESS1,
            BookingStep.PURCHASER_ADDRESS2, BookingStep.PURCHASER_CITY, BookingStep.PURCHASER_PROVINCE,
            BookingStep.PURCHASER_POSTCODE, BookingStep.PURCHASER_COUNTRY, BookingStep.TRAVEL_DATE,
            BookingStep.TRAVEL_TIME, BookingStep.NEXT_OF_KIN
        ]:
            return self._handle_purchaser_details(user_message, session_context, step)
        if step == BookingStep.BOOKING_SUMMARY: return await self._handle_booking_confirmation_enhanced(user_message, session_context)
        return self._handle_main_menu(user_message, session_context)

    def _handle_main_menu(self, message: str, session_context: Dict) -> Dict:
        if (message.lower().strip() in ["1", "bus ticketing", "yes", "start booking", "book"] or
            self._is_numeric_selection(message) == 1):
            resp = """BUS TICKETING

Where are you departing from?
- You can type a city name or a geohash (preferred).

QUICK LINKS
0 Back to Main Menu"""
            return {"response": resp, "new_step": BookingStep.DEPARTURE_SELECTION, "mode": "menu"}
        resp = """Welcome to National Tickets how may i be of assistance?
1. Bus Ticketing
2. Airline Ticketing
3. Helpdesk
4. Contacts"""
        return {"response": resp, "new_step": BookingStep.MAIN_MENU, "mode": "menu"}

    def _handle_departure_free_text(self, message: str, session_context: Dict) -> Dict:
        if self._is_numeric_selection(message) == 0:
            return self._handle_main_menu(message, session_context)
        text = message.strip()
        if not text:
            return {"response": "Please enter your departure location (city or geohash):", "new_step": BookingStep.DEPARTURE_SELECTION, "mode": "menu"}
        booking = session_context["booking_data"]
        if self._is_geohash(text):
            booking["departure_geohash"] = text.lower()
            booking["departure_input"] = text
        else:
            booking["departure_city"] = text
            booking["departure_input"] = text
        resp = """Great. Where are you traveling to?
- Type the destination city (or geohash if you prefer)."""
        return {"response": resp, "new_step": BookingStep.DESTINATION_SELECTION, "mode": "menu"}

    def _handle_destination_free_text(self, message: str, session_context: Dict) -> Dict:
        if self._is_numeric_selection(message) == 0:
            return self._handle_main_menu(message, session_context)
        text = message.strip()
        if not text:
            return {"response": "Please enter your destination (city or geohash):", "new_step": BookingStep.DESTINATION_SELECTION, "mode": "menu"}
        booking = session_context["booking_data"]
        if self._is_geohash(text):
            booking["destination_geohash"] = text.lower()
            booking["destination_input"] = text
        else:
            booking["destination_city"] = text
            booking["destination_input"] = text

        ops_list = self._format_operator_list()
        resp = f"""Please select your desired operator:

{ops_list}

QUICK LINKS
0 Back to Main Menu"""
        return {"response": resp, "new_step": BookingStep.OPERATOR_SELECTION, "mode": "menu"}

    def _handle_operator_selection(self, message: str, session_context: Dict) -> Dict:
        numeric = self._is_numeric_selection(message)
        if numeric == 0:
            return self._handle_main_menu(message, session_context)
        ops = KNOWLEDGE_SKELETON.get("operators", [])
        if numeric and 1 <= numeric <= len(ops):
            selected = ops[numeric - 1]
            booking = session_context["booking_data"]
            booking["operator"] = selected
            booking["fare"] = booking.get("fare", "Price on request")
            resp = f"""✅ {selected} Selected
Fare: {booking['fare']}

Please select your preferred payment method:

1. Bank Transfer
2. EcoCash USD
3. InnBucks
4. Complete Booking (Offline Payment)

QUICK LINKS
0 Back to Main Menu"""
            return {"response": resp, "new_step": BookingStep.PAYMENT_METHOD_SELECTION, "mode": "menu"}
        resp = f"""Invalid selection. Please choose a valid operator number (1-{len(ops)}):

{self._format_operator_list()}

QUICK LINKS
0 Back to Main Menu"""
        return {"response": resp, "new_step": BookingStep.OPERATOR_SELECTION, "mode": "menu"}

    def _is_numeric_selection(self, message: str) -> Optional[int]:
        msg = message.strip()
        return int(msg) if msg.isdigit() else None

    def _generate_ticket(self, booking: Dict, session_id: str) -> Dict:
        self.booking_counter += 1
        ts = datetime.now().strftime("%Y%m%d%H%M")
        return {
            "ticket_number": f"NT{self.booking_counter:04d}",
            "reference_code": f"REF{ts}{session_id[-4:].upper()}",
            "booking_data": booking.copy(),
            "issued_at": datetime.now().isoformat(),
            "status": "confirmed"
        }

    def _build_conversational_prompt(self, user_message: str, intent: str, entities: Dict, context: Dict, web_search_results: Any) -> str:
        base = f"""You are Melissa, a professional customer service representative for National Tickets Global.

Company Information:
- Name: {context['company']['name']}
- Location: {context['company']['location']}
- Services: {', '.join(context['company']['services'])}
- Facebook: {context['company']['social']}

User Message: "{user_message}"
Intent: {intent}

Available Data:
{json.dumps(context.get('relevant_data', {}), indent=2)}

Guidelines:
1. Be professional, helpful, and friendly
2. Provide accurate information about routes, prices, and policies
3. If asked about booking, guide users to use menu-driven booking (respond with "Let me help you with a booking. Type '1' to start.")
4. Keep responses concise but informative
5. Always identify yourself as Melissa from National Tickets Global
6. If you don't know specific information, refer to company contact methods

Web Search Results: {web_search_results if web_search_results else 'None available'}

Provide a helpful response:"""
        return base

    async def generate_response(self, user_message: str, session_id: str, intent: str, entities: Dict) -> Dict:
        start = time.time()
        self._initialize_session(session_id)
        session_context = self.conversation_context[session_id]
        current_state = session_context["conversation_state"]
        session_context["message_count"] = session_context.get("message_count", 0) + 1
        logger.info(f"[{session_id}] State: {current_state.value}, Message: '{user_message}'")

        try:
            self.conversation_memory.add_message(session_id=session_id, role=MessageRole.USER, content=user_message, intent=intent, entities=entities)
        except Exception as e:
            logger.error(f"Error storing user message: {e}")

        result = {}
        in_flow = self._is_in_booking_flow(session_context)
        stay_conv = self._should_stay_conversational(intent, user_message, session_context)
        use_menu = self._should_use_menu_mode(intent, entities, user_message, session_context)
        logger.info(f"[{session_id}] Decision factors - In booking flow: {in_flow}, Stay conversational: {stay_conv}, Use menu: {use_menu}")

        if in_flow and stay_conv:
            session_context["conversation_state"] = ConversationState.CONVERSATIONAL
            session_context["booking_step"] = None
            r = self._generate_conversational_response(user_message, session_id, intent, entities)
            result.update(r)
            result["response_type"] = "conversational_ai"
            result["state_transition"] = "menu_to_conversational_forced"
            self.response_analytics["state_transitions"] += 1
            self.response_analytics["conversational_responses"] += 1
        elif in_flow:
            r = await self._handle_menu_driven_flow(user_message, session_context)
            result.update(r)
            result["response_type"] = "structured_menu"
            self.response_analytics["menu_driven_responses"] += 1
        elif use_menu and not stay_conv:
            session_context["conversation_state"] = ConversationState.BOOKING_MENU
            session_context["booking_step"] = BookingStep.MAIN_MENU
            r = await self._handle_menu_driven_flow(user_message, session_context)
            result.update(r)
            result["response_type"] = "structured_menu"
            result["state_transition"] = "conversational_to_menu"
            self.response_analytics["state_transitions"] += 1
            self.response_analytics["menu_driven_responses"] += 1
        else:
            r = self._generate_conversational_response(user_message, session_id, intent, entities)
            result.update(r)
            result["response_type"] = "conversational_ai"
            self.response_analytics["conversational_responses"] += 1

        if result.get("new_step"):
            session_context["booking_step"] = result["new_step"]
        if result.get("new_state"):
            session_context["conversation_state"] = result["new_state"]

        rt_ms = int((time.time() - start) * 1000)
        try:
            self.conversation_memory.add_message(session_id=session_id, role=MessageRole.MELISSA, content=result.get("response", ""), response_time_ms=rt_ms)
        except Exception as e:
            logger.error(f"Error storing AI response: {e}")

        final = {
            "response": result.get("response", "I'm here to help! How can I assist you today?"),
            "conversation_state": session_context["conversation_state"].value,
            "booking_step": session_context["booking_step"].value if session_context["booking_step"] else None,
            "response_type": result.get("response_type", "conversational_ai"),
            "booking_complete": result.get("booking_complete", False),
            "state_transition": result.get("state_transition"),
            "intent": intent,
            "entities": entities,
            "session_id": session_id,
            "response_time_ms": rt_ms,
            "web_search_used": result.get("web_search_used", False),
            "mode": result.get("mode", "conversational"),
            "api_integration": False,
            "booking_result": result.get("booking_result"),
            "debug_info": {
                "previous_state": current_state.value,
                "current_state": session_context["conversation_state"].value,
                "booking_data": session_context.get("booking_data", {}),
                "menu_vs_conversational": "menu" if session_context["conversation_state"] == ConversationState.BOOKING_MENU else "conversational",
                "is_in_booking_flow": in_flow,
                "should_stay_conversational": stay_conv,
                "should_use_menu": use_menu,
                "api_calls_made": self.response_analytics["api_calls"]
            }
        }
        if result.get("booking_complete"):
            try:
                self.learning_service.mark_conversation_success(session_id, "booking_completed")
            except Exception as e:
                logger.error(f"Error marking conversation successful: {e}")

        self.response_analytics["total_responses"] += 1
        return final

    async def close(self):
        if self.busbud_client:
            await self.busbud_client.__aexit__(None, None, None)


_global_engine = None


async def generate_ai_response(user_message: str, session_id: str, gemini_client=None) -> Dict[str, Any]:
    global _global_engine
    start = time.time()
    if user_message.lower().strip() in ["hi", "hello", "start", "restart"]:
        user_message = "hi"
    intent_result = detect_intent(user_message)
    intent = intent_result.get("intent", "general")
    entities = intent_result.get("entities", {})
    if _global_engine is None:
        _global_engine = UnifiedBookingEngine(gemini_client)
        logger.info("Created global unified engine instance (free-text origin/destination)")
    result = await _global_engine.generate_response(user_message, session_id, intent, entities)
    result["total_processing_time_ms"] = int((time.time() - start) * 1000)
    return result


async def close_global_engine():
    global _global_engine
    if _global_engine:
        await _global_engine.close()
        _global_engine = None


def mark_conversation_successful(session_id: str, success_type: str = "booking_completed") -> bool:
    global _global_engine
    try:
        if _global_engine:
            return _global_engine.learning_service.mark_conversation_success(session_id, success_type)
        return False
    except Exception as e:
        logger.error(f"Error marking conversation successful: {e}")
        return False


def get_response_analytics() -> Dict[str, Any]:
    global _global_engine
    try:
        if _global_engine:
            analytics = _global_engine.response_analytics.copy()
            analytics["learning_stats"] = _global_engine.learning_service.get_learning_statistics()
            analytics["conversation_stats"] = _global_engine.conversation_memory.get_conversation_analytics()
            return analytics
        return {"error": "Engine not initialized"}
    except Exception as e:
        logger.error(f"Error getting analytics: {e}")
        return {"error": str(e)}


def get_session_state(session_id: str) -> Dict[str, Any]:
    global _global_engine
    if _global_engine and session_id in _global_engine.conversation_context:
        ctx = _global_engine.conversation_context[session_id]
        return {
            "conversation_state": ctx["conversation_state"].value,
            "booking_step": ctx["booking_step"].value if ctx["booking_step"] else None,
            "booking_data": ctx.get("booking_data", {}),
            "session_id": ctx["session_id"]
        }
    return {"error": "Session not found"}


def reset_session_state(session_id: str) -> bool:
    global _global_engine
    try:
        if _global_engine and session_id in _global_engine.conversation_context:
            del _global_engine.conversation_context[session_id]
            logger.info(f"Reset session state for: {session_id}")
            return True
        return False
    except Exception as e:
        logger.error(f"Error resetting session: {e}")
        return False

