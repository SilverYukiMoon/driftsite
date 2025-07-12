from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException, Depends, status
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy import create_engine, insert, select
from database import Base, DATABASE_URL, database, PermitApplication
from typing import List, Optional
from starlette.middleware.sessions import SessionMiddleware
from datetime import datetime
import httpx
import os
import json
import uuid
from dotenv import load_dotenv

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
DISCORD_API_BASE = "https://discord.com/api"
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")

ALLOWED_ROLE_IDS = {
    "1362205859215839322",
    "1362212187145506956"
}

os.makedirs("uploaded_permit_files", exist_ok=True)

app = FastAPI()

app.mount("/uploaded_permit_files", StaticFiles(directory="uploaded_permit_files"), name="uploaded_permit_files")
app.mount("/static", StaticFiles(directory="static"), name="static")

app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET_KEY", "your_secret_key_here"))

templates = Jinja2Templates(directory="templates")

# Sync DB engine for creating tables
SYNC_DATABASE_URL = DATABASE_URL.replace("+aiosqlite", "")
engine_sync = create_engine(SYNC_DATABASE_URL, connect_args={"check_same_thread": False})
Base.metadata.create_all(bind=engine_sync)

@app.on_event("startup")
async def startup():
    await database.connect()

@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()

# ----- Auth Helpers -----

async def get_discord_user(session: dict):
    access_token = session.get("access_token")
    if not access_token:
        return None

    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient() as client:
        user_resp = await client.get(f"{DISCORD_API_BASE}/users/@me", headers=headers)
        if user_resp.status_code != 200:
            return None
        return user_resp.json()

def require_login(request: Request):
    user = request.session.get("user")
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return user

def require_admin_roles(request: Request):
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in")
    roles = set(user.get("roles", []))
    if not ALLOWED_ROLE_IDS.intersection(roles):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    return user

# ----- Auth Routes -----

@app.get("/login")
async def login():
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify guilds.members.read"
    }
    url = f"{DISCORD_API_BASE}/oauth2/authorize?" + "&".join(f"{k}={v}" for k, v in params.items())
    return RedirectResponse(url)

@app.get("/auth/discord/callback")
async def callback(request: Request, code: Optional[str] = None):
    if not code:
        return RedirectResponse(url="/login")

    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "scope": "identify guilds.members.read"
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(f"{DISCORD_API_BASE}/oauth2/token", data=data, headers=headers)
        if token_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to get token from Discord")
        token_json = token_resp.json()
        access_token = token_json["access_token"]

        user_resp = await client.get(f"{DISCORD_API_BASE}/users/@me", headers={"Authorization": f"Bearer {access_token}"})
        if user_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to get user info from Discord")
        user_json = user_resp.json()

    user_id = user_json["id"]
    bot_token = os.getenv("DISCORD_BOT_TOKEN")

    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient() as client:
        guild_member_resp = await client.get(
            f"{DISCORD_API_BASE}/guilds/{DISCORD_GUILD_ID}/members/{user_id}",
            headers=headers
        )

    if guild_member_resp.status_code != 200:
        return RedirectResponse(url="/login")

    guild_member_json = guild_member_resp.json()
    roles = guild_member_json.get("roles", [])

    request.session["access_token"] = access_token
    request.session["user"] = {
        "id": user_json["id"],
        "username": user_json["username"],
        "discriminator": user_json["discriminator"],
        "avatar": user_json.get("avatar"),
        "roles": roles
    }

    return RedirectResponse(url="/admin")

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/")

# ----- Admin Dashboard -----

@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request, user: dict = Depends(require_admin_roles)):
    return templates.TemplateResponse("admin_dashboard.html", {
        "request": request,
        "user": user
    })

@app.post("/admin/server/start", dependencies=[Depends(require_admin_roles)])
async def start_server():
    success = start_tickhosting_server()
    return "Server started!" if success else "Failed to start server."

@app.post("/admin/server/stop", dependencies=[Depends(require_admin_roles)])
async def stop_server():
    success = stop_tickhosting_server()
    return "Server stopped!" if success else "Failed to stop server."

def start_tickhosting_server():
    print("Starting server... (replace with real logic)")
    return True

def stop_tickhosting_server():
    print("Stopping server... (replace with real logic)")
    return True

# ----- Permit Submission -----

@app.post("/submit-permit")
async def submit_permit(
    request: Request,
    full_name: str = Form(...),
    alias: Optional[str] = Form(None),
    crew: Optional[str] = Form(None),
    contact_address: Optional[str] = Form(None),
    preferred_contact: Optional[str] = Form(None),
    other_corr_text: Optional[str] = Form(None),
    permit_type: str = Form(...),
    other_permit_text: Optional[str] = Form(None),
    permit_details: Optional[str] = Form(None),
    applicant_signature: str = Form(...),
    application_date: str = Form(...),
    supporting_files: Optional[List[UploadFile]] = File(None)
):
    os.makedirs("uploaded_permit_files", exist_ok=True)
    saved_files = []

    if supporting_files:
        for upload in supporting_files:
            if upload.filename:
                safe_filename = os.path.basename(upload.filename)
                unique_filename = f"{uuid.uuid4().hex}_{safe_filename}"
                file_path = os.path.join("uploaded_permit_files", unique_filename)
                with open(file_path, "wb") as f:
                    f.write(await upload.read())
                saved_files.append(unique_filename)

    supporting_files_json = json.dumps(saved_files) if saved_files else None

    try:
        parsed_application_date = datetime.fromisoformat(application_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format")

    query = insert(PermitApplication).values(
        full_name=full_name,
        alias=alias,
        crew=crew,
        contact_address=contact_address,
        preferred_contact=preferred_contact,
        other_corr_text=other_corr_text,
        permit_type=permit_type,
        other_permit_text=other_permit_text,
        permit_details=permit_details,
        applicant_signature=applicant_signature,
        application_date=parsed_application_date,
        supporting_files=supporting_files_json
    )

    await database.execute(query)

    return templates.TemplateResponse("submission_success.html", {
        "request": request,
        "full_name": full_name,
        "permit_type": permit_type,
        "crew": crew,
        "supporting_files": saved_files
    })

# ----- Admin View Applications -----

@app.get("/admin")
async def admin_page(request: Request):
    user = request.session.get("user")
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    query = select(PermitApplication).order_by(PermitApplication.application_date.desc())
    rows = await database.fetch_all(query)

    applications = []
    for row in rows:
        app_dict = dict(row)
        if app_dict.get("supporting_files"):
            try:
                app_dict["supporting_files"] = json.loads(app_dict["supporting_files"])
            except json.JSONDecodeError:
                app_dict["supporting_files"] = []
        else:
            app_dict["supporting_files"] = []
        applications.append(app_dict)

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "user": user,
        "applications": applications
    })

@app.get("/admin/app/{application_id}")
async def view_application(request: Request, application_id: int):
    user = request.session.get("user")
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    query = select(PermitApplication).where(PermitApplication.id == application_id)
    app_data = await database.fetch_one(query)

    if not app_data:
        raise HTTPException(status_code=404, detail="Application not found")

    app_dict = dict(app_data)
    raw_files = app_dict.get("supporting_files")
    if raw_files:
        try:
            app_dict["supporting_files"] = json.loads(raw_files)
        except json.JSONDecodeError:
            app_dict["supporting_files"] = []
    else:
        app_dict["supporting_files"] = []

    return templates.TemplateResponse("admin_application_detail.html", {
        "request": request,
        "user": user,
        "app": app_dict
    })

laws_data = [
    {
        "id": "hat-compliance-act",
        "title": "The Hat Compliance Act",
        "sections": {
            "Section 1.01": "All individuals engaging in piratical activities within the jurisdiction of Aurospan shall wear headwear registered with the Corsair Council.",
            "Section 1.02": "Registered hats must meet size, style, and enchantment specifications as outlined in Appendix H-17B.",
            "Section 1.03": "Failure to comply shall result in immediate revocation of pirate status and associated legal protections, with penalties including but not limited to fines, hat confiscation, and compulsory attendance at the Hat Compliance Re-education Program."
        }
    },
    {
        "id": "letter-of-marque-enforcement-law",
        "title": "The Letter of Marque Enforcement Law",
        "sections": {
            "Section 2.01": "No vessel shall engage in acts of privateering, including but not limited to boarding, raiding, or seizure of other ships, without a valid and current Letter of Marque issued by the Council.",
            "Section 2.02": "Letters of Marque must specify authorized targets, temporal limits, and operational regions.",
            "Section 2.03": "Unauthorized engagements shall be prosecuted as acts of piracy under the Maritime Criminal Code, subject to seizure, fines, and imprisonment."
        }
    },
    {
        "id": "bureaucratic-waters-jurisdiction-rule",
        "title": "The Bureaucratic Waters Jurisdiction Rule",
        "sections": {
            "Section 3.01": "The territorial waters of Aurospan are divided into designated bureaucratic zones, each governed by distinct sets of laws and regulations.",
            "Section 3.02": "Vessels crossing jurisdictional meridians must immediately comply with the applicable laws of the new zone.",
            "Section 3.03": "Ignorance of zone boundaries shall not be accepted as a defense against legal enforcement or prosecution."
        }
    },
    {
        "id": "magical-spellcasting-registration-act",
        "title": "The Magical Spellcasting Registration Act",
        "sections": {
            "Section 4.01": "All magical spellcasting conducted aboard vessels or within port jurisdictions requires possession of a valid Magic Usage Permit.",
            "Section 4.02": "Permits must specify the types of spells authorized, duration, and frequency of use.",
            "Section 4.03": "Unauthorized or unregistered magical activity shall result in penalties including fines, suspension of magical privileges, and possible magical bindings."
        }
    },
    {
        "id": "explosives-handling-and-safety-code",
        "title": "The Explosives Handling and Safety Code",
        "sections": {
            "Section 5.01": "Ownership, transport, storage, and use of explosive devices including but not limited to cannons, bombs, and incendiaries require an Explosive Handling Permit.",
            "Section 5.02": "All explosives must be inspected and approved by licensed Safety Inspectors prior to use.",
            "Section 5.03": "Violations of this code shall be subject to confiscation of explosives, monetary fines, and increased insurance premiums."
        }
    },
    {
        "id": "crew-manifest-registration-law",
        "title": "The Crew Manifest Registration Law",
        "sections": {
            "Section 6.01": "Every vessel must maintain a current crew manifest listing all personnel, including name, role, and legal status.",
            "Section 6.02": "Manifests must be submitted quarterly to the Council’s Registry Office.",
            "Section 6.03": "Failure to submit or falsification of crew manifests shall be punishable by fines, detention of vessel, and revocation of operating licenses."
        }
    },
    {
        "id": "trade-and-tariff-regulation",
        "title": "The Trade and Tariff Regulation",
        "sections": {
            "Section 7.01": "All goods imported, exported, or traded within Aurospan’s jurisdiction must be declared with a valid Trade Permit.",
            "Section 7.02": "Tariffs will be applied based on goods classification, value, and origin, as outlined in the Tariff Schedule Appendix T-4.",
            "Section 7.03": "Undeclared or smuggled goods are subject to immediate seizure and penalties including fines and potential imprisonment."
        }
    },
    {
        "id": "harbor-docking-and-repair-ordinance",
        "title": "The Harbor Docking and Repair Ordinance",
        "sections": {
            "Section 8.01": "Vessels shall obtain Repair and Docking Permits prior to mooring or undertaking repairs in any port under Council jurisdiction.",
            "Section 8.02": "Repairs performed without permits will incur a Rust Tax and may lead to denied future docking privileges.",
            "Section 8.03": "Harbor authorities are empowered to enforce compliance and report violations to the Council."
        }
    },
    {
        "id": "duel-authorization-and-conduct-act",
        "title": "The Duel Authorization and Conduct Act",
        "sections": {
            "Section 9.01": "Formal dueling activities must be pre-authorized through submission of a Duel Consent Form.",
            "Section 9.02": "Duels conducted without authorization are illegal and subject to legal action.",
            "Section 9.03": "Approved duels must abide by Council-regulated rules and be supervised by a licensed referee."
        }
    },
    {
        "id": "waste-and-environmental-protection-law",
        "title": "The Waste and Environmental Protection Law",
        "sections": {
            "Section 10.01": "Disposal of refuse, magical residues, and hazardous materials into maritime environments is strictly regulated.",
            "Section 10.02": "Disposal requires a Waste Disposal Permit and adherence to environmental protection standards.",
            "Section 10.03": "Violations may result in fines, mandated cleanup efforts, and suspension of docking privileges."
        }
    },
    {
        "id": "communications-regulation-statute",
        "title": "The Communications Regulation Statute",
        "sections": {
            "Section 11.01": "Operation of magical or mundane communication devices requires possession of a valid Communications License.",
            "Section 11.02": "Communications logs must be maintained and submitted monthly.",
            "Section 11.03": "Unauthorized transmissions are subject to interception and fines."
        }
    },
    {
        "id": "salvage-and-wreckage-rights-law",
        "title": "The Salvage and Wreckage Rights Law",
        "sections": {
            "Section 12.01": "Salvage operations require explicit authorization and must comply with environmental and safety regulations.",
            "Section 12.02": "Unauthorized salvage constitutes theft and is punishable by confiscation of recovered goods and fines.",
            "Section 12.03": "Salvage crews must submit detailed reports post-operation."
        }
    },
    {
        "id": "smuggling-prohibition-act",
        "title": "The Smuggling Prohibition Act",
        "sections": {
            "Section 13.01": "Transport of banned or controlled goods without a valid Smuggling Exemption Certificate is prohibited.",
            "Section 13.02": "Violators will face confiscation of goods, heavy fines, and possible imprisonment.",
            "Section 13.03": "Cooperation with Council investigations may result in temporary immunity."
        }
    },
    {
        "id": "embassy-recognition-and-conduct-code",
        "title": "The Embassy Recognition and Conduct Code",
        "sections": {
            "Section 14.01": "Pirate embassies must comply with diplomatic protocols and maintain proper permits.",
            "Section 14.02": "Violations may result in suspension of diplomatic status and sanctions.",
            "Section 14.03": "Embassies are responsible for the conduct of their representatives."
        }
    },
    {
        "id": "magical-artifact-possession-regulation",
        "title": "The Magical Artifact Possession Regulation",
        "sections": {
            "Section 15.01": "Possession, trade, or use of magical artifacts requires licensing and registration with the Council.",
            "Section 15.02": "Unlicensed artifacts are subject to confiscation and nullification.",
            "Section 15.03": "Owners must disclose all magical properties and origins."
        }
    },
    {
        "id": "monster-control-and-handling-law",
        "title": "The Monster Control and Handling Law",
        "sections": {
            "Section 16.01": "Capture, taming, or use of magical sea creatures requires proper permits and adherence to safety protocols.",
            "Section 16.02": "Unauthorized handling is illegal and subject to penalties including confiscation and fines."
        }
    },
    {
        "id": "currency-and-coinage-control-act",
        "title": "The Currency and Coinage Control Act",
        "sections": {
            "Section 17.01": "Minting, exchange, and transport of currency are regulated activities requiring appropriate licensing.",
            "Section 17.02": "Counterfeit or unregistered currency is illegal and subject to criminal prosecution.",
            "Section 17.03": "Currency audits shall be conducted periodically."
        }
    },
    {
        "id": "petition-and-legal-filing-rule",
        "title": "The Petition and Legal Filing Rule",
        "sections": {
            "Section 18.01": "Formal legal filings and petitions require submission of a Petition Filing Permit.",
            "Section 18.02": "Frivolous or repetitive petitions may be dismissed and fined.",
            "Section 18.03": "All filings must be on approved parchment and submitted in triplicate."
        }
    },
    {
        "id": "parade-and-public-assembly-ordinance",
        "title": "The Parade and Public Assembly Ordinance",
        "sections": {
            "Section 19.01": "Public assemblies, including celebrations, protests, and riots, require prior approval and permits.",
            "Section 19.02": "Unpermitted gatherings may be dispersed by authorities and fined.",
            "Section 19.03": "Explosions or disturbances during events require immediate additional permits and may incur penalties."
        }
    },
    {
        "id": "fog-navigation-and-hazard-exemption-law",
        "title": "The Fog Navigation and Hazard Exemption Law",
        "sections": {
            "Section 20.01": "Travel through magically obscured or dangerous waters requires a Fog Navigation Exemption Permit.",
            "Section 20.02": "Failure to obtain this permit may result in vessel detention or fines.",
            "Section 20.03": "Captains must submit navigation plans and magical weather forecasts prior to passage."
        }
    }
]

from datetime import datetime

documents_data = [
    {
        "id": "great-plunder-charter",
        "title": "The Great Plunder Charter",
        "description": "The foundational treaty that first granted legal recognition to privateering under complex and contradictory terms, including the infamous “hat size” clause.",
        "ratification_date": datetime(1725, 3, 15),
        "parties": ["The Corsair Council", "The Kingdom of Valderon"],
        "text": """\
Article I: Privateering is hereby recognized as a lawful enterprise within the jurisdictional waters designated herein.

Article II: The 'Hat Size Clause' states that all captains must wear hats with brim sizes no less than 7 inches for official recognition.

Article III: Violations of this charter shall result in revocation of privileges and possible imprisonment.

This charter is binding upon all signatories and shall be renewed every ten years unless amended by mutual consent.""",
        "signatories": [
            {"name": "Captain Redbeard", "title": "Council Representative"},
            {"name": "Governor Thalia Valderon", "title": "Kingdom of Valderon"}
        ],
        "seal": ""
    },
    {
        "id": "letters-of-marque-and-reprisal",
        "title": "Letters of Marque and Reprisal",
        "description": "Individual licenses issued to crews authorizing them to attack enemy vessels; each letter is unique and often heavily amended or contested.",
        "ratification_date": datetime(1731, 6, 22),
        "parties": ["The Corsair Council", "Various Authorized Crews"],
        "text": """\
Clause 1: Letters of Marque authorize the bearer to engage enemy vessels as defined under wartime conditions.

Clause 2: Each letter shall specify the enemy factions and permitted spoils.

Clause 3: Bearers must present letters upon request to avoid being treated as outlaws.

Clause 4: All disputes concerning letters shall be brought before the Maritime Arbitration Pact.""",
        "signatories": [
            {"name": "Admiral Lorna", "title": "Council Seal Bearer"},
            {"name": "Captain Silverfin", "title": "Recipient Crew Leader"}
        ],
        "seal": ""
    },
    {
        "id": "sovereign-pirate-accord",
        "title": "The Sovereign Pirate Accord",
        "description": "A multi-faction treaty establishing diplomatic protocols between pirate crews, merchant states, and magical guilds, including rules for embassies and conflict resolution.",
        "ratification_date": datetime(1740, 11, 5),
        "parties": ["Pirate Crews Collective", "Merchant States Consortium", "Arcane Guild Council"],
        "text": """\
Section 1: Establishes recognition of pirate embassies and their immunities.

Section 2: Defines conflict resolution methods favoring arbitration and magical mediation.

Section 3: Prohibits unauthorized acts of war within designated diplomatic zones.

Section 4: Enforces shared taxation protocols on goods traded through neutral ports.""",
        "signatories": [
            {"name": "Envoy Kera", "title": "Pirate Crews Collective"},
            {"name": "Merchant Lord Velos", "title": "Merchant States Consortium"},
            {"name": "Archmage Thallis", "title": "Arcane Guild Council"}
        ],
        "seal": ""
    },
    {
        "id": "councils-edicts",
        "title": "The Council’s Edicts",
        "description": "Binding legal rulings issued by the Council that interpret, amend, or suspend existing laws and treaties; often contradictory and subject to reinterpretation.",
        "ratification_date": None,
        "parties": ["The Corsair Council"],
        "text": """\
Edict 001: Suspension of all smuggling activities within Council waters.

Edict 002: Amendment of the Hat Protocol Declaration to allow feather adornments.

Edict 003: Temporary halt on duel consent enforcement during states of emergency.

Edict 004: Clarification on tax rates for magical artifacts.""",
        "signatories": [
            {"name": "Council Chairwoman Maelra", "title": "The Corsair Council"}
        ],
        "seal": ""
    },
    {
        "id": "treaty-of-red-tape",
        "title": "The Treaty of Red Tape",
        "description": "An agreement outlining the boundaries and jurisdiction of bureaucratic waters, defining where certain laws and permits apply or lapse.",
        "ratification_date": datetime(1728, 9, 30),
        "parties": ["Bureaucratic Syndicate", "Corsair Council"],
        "text": """\
Article I: Defines bureaucratic waters jurisdiction and permit requirements.

Article II: Specifies permit expiration and renewal procedures.

Article III: Establishes a fines system for violation of jurisdiction boundaries.

Article IV: Provides guidelines for dispute arbitration between overlapping jurisdictions.""",
        "signatories": [
            {"name": "Registrar Faelin", "title": "Bureaucratic Syndicate"},
            {"name": "Captain Ironhook", "title": "Corsair Council"}
        ],
        "seal": ""
    },
    {
        "id": "hat-protocol-declaration",
        "title": "The Hat Protocol Declaration",
        "description": "The official legal document codifying the use and registration of hats as symbols of pirate status and legal identity.",
        "ratification_date": datetime(1735, 1, 12),
        "parties": ["The Corsair Council"],
        "text": """\
Clause 1: Registration of hats is mandatory for all captains.

Clause 2: Hat designs and adornments must be approved by the Council.

Clause 3: Wearing unregistered hats will result in fines or loss of privileges.

Clause 4: The Hat Registry will be maintained at the Council’s headquarters.""",
        "signatories": [
            {"name": "Registrar Faelin", "title": "The Corsair Council"}
        ],
        "seal": ""
    },
    {
        "id": "magical-seal-codex",
        "title": "The Magical Seal Codex",
        "description": "A compendium of approved magical seals, their legal effects, and protocols for their use in contracts and spellcasting.",
        "ratification_date": datetime(1742, 4, 21),
        "parties": ["Arcane Guild Council", "The Corsair Council"],
        "text": """\
Section A: List of approved magical seals and their effects.

Section B: Protocols for affixing seals to legal documents.

Section C: Penalties for unauthorized seal use.

Section D: Procedures for seal revocation and renewal.""",
        "signatories": [
            {"name": "Archmage Thallis", "title": "Arcane Guild Council"},
            {"name": "Council Chairwoman Maelra", "title": "The Corsair Council"}
        ],
        "seal": ""
    },
    {
        "id": "maritime-arbitration-pact",
        "title": "The Maritime Arbitration Pact",
        "description": "An agreement mandating arbitration instead of violence for certain disputes between recognized pirate factions and states.",
        "ratification_date": datetime(1745, 7, 17),
        "parties": ["Pirate Factions", "Merchant States"],
        "text": """\
Article I: All disputes falling under this pact shall be settled by designated arbitrators.

Article II: Arbitrators will be selected jointly by disputing parties.

Article III: Arbitration outcomes are binding and enforceable by all signatories.

Article IV: Violation of arbitration decisions will result in sanctions.""",
        "signatories": [
            {"name": "Captain Blackwing", "title": "Pirate Factions"},
            {"name": "Ambassador Lystra", "title": "Merchant States"}
        ],
        "seal": ""
    },
    {
        "id": "binding-oath-of-plunder",
        "title": "The Binding Oath of Plunder",
        "description": "A formal contract sworn by captains and crews affirming allegiance to the Council and acceptance of its laws, often used to settle disputes or grant special privileges.",
        "ratification_date": datetime(1738, 2, 3),
        "parties": ["Pirate Captains", "The Corsair Council"],
        "text": """\
Clause 1: All signatories pledge loyalty to the Council’s laws.

Clause 2: Breach of oath results in trial by the Council’s judiciary.

Clause 3: Privileges granted under this oath include safe harbor and trade rights.

Clause 4: The oath must be renewed upon change of crew leadership.""",
        "signatories": [
            {"name": "Captain Redbeard", "title": "Pirate Captains"},
            {"name": "Council Chairwoman Maelra", "title": "The Corsair Council"}
        ],
        "seal": ""
    },
    {
        "id": "smugglers-amnesty-decree",
        "title": "The Smuggler’s Amnesty Decree",
        "description": "A legal ruling granting temporary immunity to smugglers who cooperate with Council investigations or who file proper smuggling exemption permits.",
        "ratification_date": datetime(1747, 11, 11),
        "parties": ["The Corsair Council", "Smugglers' Coalition"],
        "text": """\
Section 1: Amnesty applies only to those filing required exemption permits.

Section 2: Cooperation with investigations is mandatory.

Section 3: Amnesty is revoked if smuggling resumes without permit.

Section 4: Records of amnesty holders shall be maintained confidentially.""",
        "signatories": [
            {"name": "Registrar Faelin", "title": "The Corsair Council"},
            {"name": "Smuggler King Vrax", "title": "Smugglers' Coalition"}
        ],
        "seal": ""
    },
    {
        "id": "embargo-directive",
        "title": "The Embargo Directive",
        "description": "A document declaring trade sanctions or port closures against specific crews, nations, or factions for violations of Council laws.",
        "ratification_date": datetime(1749, 5, 20),
        "parties": ["The Corsair Council"],
        "text": """\
Article I: Ports named herein shall close trade to sanctioned entities.

Article II: Sanctioned parties are barred from Council waters.

Article III: Violations may result in armed enforcement.

Article IV: Sanctions may be lifted upon petition and review.""",
        "signatories": [
            {"name": "Council Chairwoman Maelra", "title": "The Corsair Council"}
        ],
        "seal": ""
    },
    {
        "id": "curse-remission-act",
        "title": "The Curse Remission Act",
        "description": "A legal document that nullifies certain magical curses or enchantments, often requiring complex filings and hefty fees.",
        "ratification_date": datetime(1751, 3, 30),
        "parties": ["The Corsair Council", "Arcane Guild Council"],
        "text": """\
Clause 1: Curse remission requires formal application and fee payment.

Clause 2: Review by the Arcane Guild is mandatory.

Clause 3: Only curses listed in the official registry are eligible.

Clause 4: Remission certificates must be carried at all times.""",
        "signatories": [
            {"name": "Archmage Thallis", "title": "Arcane Guild Council"},
            {"name": "Registrar Faelin", "title": "The Corsair Council"}
        ],
        "seal": ""
    },
    {
        "id": "wreck-salvage-rights-agreement",
        "title": "The Wreck Salvage Rights Agreement",
        "description": "A contract defining ownership and responsibilities related to shipwreck salvage operations within Council waters.",
        "ratification_date": datetime(1733, 8, 14),
        "parties": ["Council Salvage Commission", "Pirate Salvage Crews"],
        "text": """\
Article 1: Salvage rights are granted to the first registered claimant.

Article 2: Environmental protections must be observed.

Article 3: Salvage disputes will be resolved under Maritime Arbitration Pact.

Article 4: Salvage profits are subject to taxation by the Council.""",
        "signatories": [
            {"name": "Commissioner Harlen", "title": "Council Salvage Commission"},
            {"name": "Captain Blackwing", "title": "Pirate Salvage Crews"}
        ],
        "seal": ""
    },
    {
        "id": "duel-consent-form",
        "title": "The Duel Consent Form",
        "description": "A legal document signed before duels, specifying rules, witnesses, and consequences of the combat.",
        "ratification_date": datetime(1750, 10, 1),
        "parties": ["Dueling Parties"],
        "text": """\
Clause 1: Combatants agree to abide by duel rules set herein.

Clause 2: Witnesses must be present and sign consent.

Clause 3: The loser waives all legal claims related to the duel.

Clause 4: Council adjudication is final for disputes arising.""",
        "signatories": [],
        "seal": ""
    },
    {
        "id": "maritime-environmental-compliance-report",
        "title": "The Maritime Environmental Compliance Report",
        "description": "A formal report submitted by crews or ports detailing adherence to waste disposal and environmental regulations.",
        "ratification_date": datetime(1752, 6, 25),
        "parties": ["Environmental Watch", "Ports and Crews"],
        "text": """\
Section 1: Waste disposal must follow established Council guidelines.

Section 2: Reports must be submitted quarterly.

Section 3: Violations incur penalties as outlined in the Treaty of Red Tape.

Section 4: Compliance reports will be publicly accessible.""",
        "signatories": [],
        "seal": ""
    },
    {
        "id": "communications-treaty",
        "title": "The Communications Treaty",
        "description": "An agreement regulating magical and mundane communication between jurisdictions, including restrictions on telepathy and magical message delivery.",
        "ratification_date": datetime(1748, 12, 12),
        "parties": ["Corsair Council", "Arcane Guild Council"],
        "text": """\
Article I: Telepathic communication across jurisdictions is restricted without permit.

Article II: Magical message delivery requires registration.

Article III: Violations may result in communication blackouts.

Article IV: The Council reserves the right to inspect communication devices.""",
        "signatories": [
            {"name": "Council Chairwoman Maelra", "title": "Corsair Council"},
            {"name": "Archmage Thallis", "title": "Arcane Guild Council"}
        ],
        "seal": ""
    },
    {
        "id": "piratical-census-report",
        "title": "The Piratical Census Report",
        "description": "A detailed registry of pirate crews, ships, and notable members, used for taxation and legal recognition.",
        "ratification_date": datetime(1753, 4, 18),
        "parties": ["The Corsair Council"],
        "text": """\
Section 1: All crews must register annually.

Section 2: Ships must be documented with specifications.

Section 3: Members’ statuses and ranks shall be recorded.

Section 4: Census data is confidential and used for legal purposes only.""",
        "signatories": [
            {"name": "Registrar Faelin", "title": "The Corsair Council"}
        ],
        "seal": ""
    },
    {
        "id": "councils-legal-review-opinion",
        "title": "The Council’s Legal Review Opinion",
        "description": "A non-binding but highly influential document offering interpretations of ambiguous laws or permits, often cited in disputes.",
        "ratification_date": None,
        "parties": ["The Corsair Council"],
        "text": """\
Opinion 1: Clarifies the application of the Treaty of Red Tape in newly charted waters.

Opinion 2: Interprets the scope of the Smugglers' Amnesty Decree.

Opinion 3: Advises on the enforcement of the Magical Seal Codex.""",
        "signatories": [
            {"name": "Council Chairwoman Maelra", "title": "The Corsair Council"}
        ],
        "seal": ""
    },
    {
        "id": "mutual-defense-compact",
        "title": "The Mutual Defense Compact",
        "description": "A treaty obligating signatories to aid each other in cases of external attack, subject to complicated clauses and loopholes.",
        "ratification_date": datetime(1743, 9, 10),
        "parties": ["Signatory Pirate Factions"],
        "text": """\
Article 1: Signatories agree to provide mutual military assistance.

Article 2: Clause 7 exempts signatories during internal disputes.

Article 3: Compact is subject to annual review and renewal.

Article 4: Disputes over obligations will be settled via Maritime Arbitration.""",
        "signatories": [
            {"name": "Captain Blackwing", "title": "Pirate Factions"}
        ],
        "seal": ""
    },
    {
        "id": "embassies-establishment-charter",
        "title": "The Embassies Establishment Charter",
        "description": "A foundational document outlining the rights, privileges, and responsibilities of pirate embassies within foreign territories.",
        "ratification_date": datetime(1746, 1, 22),
        "parties": ["Corsair Council", "Foreign Powers"],
        "text": """\
Clause 1: Embassies shall have diplomatic immunity within host territories.

Clause 2: Embassies must register personnel with the host government.

Clause 3: Diplomatic disputes shall be handled through Council mediation.

Clause 4: Embassies must not interfere in host political affairs.""",
        "signatories": [
            {"name": "Council Chairwoman Maelra", "title": "Corsair Council"},
            {"name": "Ambassador Velmar", "title": "Foreign Powers"}
        ],
        "seal": ""
    }
]

# Permit data list
permits_data = [
    {
        "name": "Letter of Marque",
        "description": "Grants permission to legally attack ships designated as enemies or pirates within strict parameters.",
        "fee": "1,000 gold coins + notarization tax",
        "application": "Must specify enemy flag colors, ship sizes, and the exact window of engagement—missing details voids the permit.",
        "funny_note": "The clause about “hat size” must be included to avoid legal ambiguity."
    },
    {
        "name": "Plunder License",
        "description": "Authorizes raids on specific ports, vessels, or cargo during predefined dates and times.",
        "fee": "Variable; higher for wealthy ports",
        "application": "Requires submission of a detailed raid itinerary, including estimated number of cannonballs to be fired.",
        "funny_note": "Raids during “Hatless Days” require double paperwork."
    },
    {
        "name": "Magic Usage Permit",
        "description": "Licenses the use of spellcasting aboard ships or within port territories. Specifies allowed spell types and durations.",
        "fee": "500 gold + parchment preservation fee",
        "application": "Spell logs must be submitted quarterly, including magical ink colors used.",
        "funny_note": "Unauthorized use of “Summon Ink Spirit” spells incurs an automatic three-day suspension."
    },
    {
        "name": "Hat Registration Certificate",
        "description": "Registers pirate hats by size, style, and magical enhancements to legally define pirate status.",
        "fee": "50 gold per hat + enchantment inspection fee",
        "application": "Includes a mandatory hat-measuring ceremony, sometimes conducted by certified “Hat Inspectors.”",
        "funny_note": "Wearing unregistered hats in court may cause instant contempt charges."
    },
    {
        "name": "Trade Permit",
        "description": "Allows legal import, export, and sale of goods subject to tariffs, quotas, and inspections.",
        "fee": "Percentage of cargo value + harbor taxes",
        "application": "Must include an inventory list with exact magical residue counts if applicable.",
        "funny_note": "Smugglers often forget to declare “excessive charm” in enchanted goods."
    },
    {
        "name": "Explosive Handling Permit",
        "description": "Authorizes ownership and use of cannons, bombs, and other explosive devices.",
        "fee": "300 gold + mandatory safety inspection fee",
        "application": "Includes a written test on “Proper Filing of Detonation Forms.”",
        "funny_note": "Explosions caused by improperly filed permits may result in a personal “blast tax.”"
    },
    {
        "name": "Ship Registration and Classification",
        "description": "Certifies a ship’s seaworthiness, armament class, and official name for legal operation.",
        "fee": "200 gold + inspection fee",
        "application": "Ship name must not duplicate or rhyme with any existing vessel names.",
        "funny_note": "Ships with “overly aggressive” names require extra licensing."
    },
    {
        "name": "Crew Manifest Approval",
        "description": "Registers all crew members with their roles and legal statuses aboard a vessel.",
        "fee": "10 gold per crew member + administrative processing fee",
        "application": "Includes proof of hat registration for all listed pirates.",
        "funny_note": "Failure to list “pet parrot” as a crew member may cause legal disputes over “avian property rights.”"
    },
    {
        "name": "Navigation License",
        "description": "Authorizes captains and navigators to operate ships in specified waters or routes.",
        "fee": "150 gold + map verification fee",
        "application": "Requires passing a bureaucratic “Chart Reading and Document Filing” exam.",
        "funny_note": "Licenses revoked for failure to navigate around “bureaucratic meridians.”"
    },
    {
        "name": "Diplomatic Envoy Pass",
        "description": "Permits official representation of a pirate faction or crew at foreign courts or embassies.",
        "fee": "500 gold + diplomatic seal fee",
        "application": "Requires submission of a sworn oath of allegiance to no fewer than three different pirate lords.",
        "funny_note": "Envoys caught negotiating without a properly registered hat may be declared persona non grata."
    },
    {
        "name": "Contract Binding Seal Permit",
        "description": "Grants the right to use magical seals that validate or nullify contracts and agreements.",
        "fee": "400 gold + magical ink tax",
        "application": "Seal designs must be approved to avoid confusion with official government seals.",
        "funny_note": "Use of “invisible ink” seals without disclosure leads to automatic contract annulment."
    },
    {
        "name": "Smuggling Exemption Certificate",
        "description": "A rare legal loophole allowing transport of otherwise banned goods under specific conditions.",
        "fee": "Negotiated case-by-case",
        "application": "Requires “creative storytelling” section describing smuggling routes.",
        "funny_note": "Applicants often need to declare “excessive charm” or “persuasive speech” as smuggling tools."
    },
    {
        "name": "Repair and Docking Permit",
        "description": "Authorizes a ship to dock and undergo repairs in specified ports or drydocks.",
        "fee": "Variable by port size and repair complexity",
        "application": "Repair logs must be submitted post-docking with detailed damage descriptions.",
        "funny_note": "Repairs done without permits are subject to a “rust tax” assessed by harbor officials."
    },
    {
        "name": "Harbor Trade License",
        "description": "Allows buying or selling goods in harbor markets or aboard ships.",
        "fee": "100 gold + market stall rental fees",
        "application": "Includes “goods provenance” paperwork to verify lawful origin.",
        "funny_note": "Sellers of “enchanted hats” must submit proof of hat registration for each item."
    },
    {
        "name": "Salvage Operation Permit",
        "description": "Grants permission to recover wreckage, treasure, or cargo from shipwrecks within designated zones.",
        "fee": "250 gold + environmental impact fee",
        "application": "Includes a detailed dive plan and treasure catalog.",
        "funny_note": "Salvage crews must submit a “monster encounter” report for any sea creature interruptions."
    },
    {
        "name": "Monster Control Permit",
        "description": "Regulates the capture, taming, or use of magical sea creatures for labor or defense.",
        "fee": "600 gold + special handling surcharge",
        "application": "Includes “creature care” certification and insurance documentation.",
        "funny_note": "Monsters used without permits may be declared “undocumented magical beings” and seized."
    },
    {
        "name": "Magical Artifact License",
        "description": "Required to possess, trade, or operate enchanted items and relics.",
        "fee": "400 gold + artifact inspection fee",
        "application": "Must provide detailed magical properties and history of artifact.",
        "funny_note": "Unlicensed use of “Cursed Compass” artifacts results in automatic fine doubling."
    },
    {
        "name": "Communications License",
        "description": "Authorizes use of magical or mundane communication devices across different jurisdictions.",
        "fee": "150 gold + signal regulation fee",
        "application": "Communication logs must be submitted monthly.",
        "funny_note": "Use of “telepathic pigeons” requires additional animal handling permits."
    },
    {
        "name": "Duel Authorization Certificate",
        "description": "Legal approval to engage in formal combat or dueling under recognized rules.",
        "fee": "75 gold + referee fee",
        "application": "Includes submission of proposed duel rules and neutral referee appointment.",
        "funny_note": "Duels fought without permits may be declared “unofficial and non-binding” but still enforceable."
    },
    {
        "name": "Waste Disposal Permit",
        "description": "Regulates dumping of refuse, magical residue, or hazardous materials into the sea.",
        "fee": "200 gold + environmental protection surcharge",
        "application": "Requires detailed waste inventory and disposal plan.",
        "funny_note": "Illegal dumping may lead to “sea monster harassment” penalties."
    },
    {
        "name": "Fog Navigation Exemption",
        "description": "Permits travel through dangerous or magically obscured waters otherwise restricted.",
        "fee": "300 gold + visibility hazard fee",
        "application": "Includes detailed navigation plan and magical weather forecast.",
        "funny_note": "Failure to report “ghost ship sightings” during fog incurs heavy fines."
    },
    {
        "name": "Currency Exchange License",
        "description": "Authorizes minting, exchanging, or transporting coins, tokens, or magical currencies.",
        "fee": "250 gold + mint inspection fee",
        "application": "Includes currency origin documentation and audit reports.",
        "funny_note": "Use of counterfeit “paper hats” as currency is strictly prohibited."
    },
    {
        "name": "Petition Filing Permit",
        "description": "Required to submit formal complaints, appeals, or petitions to courts or bureaucratic offices.",
        "fee": "10 gold per filing + clerical fee",
        "application": "Petition must be handwritten in triplicate, using only official parchment.",
        "funny_note": "Petitions complaining about the Petition Filing Permit itself are automatically dismissed."
    },
    {
        "name": "Embassy Establishment Permit",
        "description": "Allows founding and operation of diplomatic outposts or embassies on foreign soil or sea.",
        "fee": "2,000 gold + diplomatic liaison fee",
        "application": "Requires full architectural plans and proof of political backing.",
        "funny_note": "Embassies without properly registered hats may be declared “illegal establishments.”"
    },
    {
        "name": "Parade and Ceremony Permit",
        "description": "Regulates public displays of power, celebrations, protests, or riots.",
        "fee": "100 gold + crowd control deposit",
        "application": "Event plans must be submitted with proposed routes and safety measures.",
        "funny_note": "Any explosions during parades require immediate additional permits and fines."
    },
    {
        "name": "Hat Dyeing and Modification License",
        "description": "Permits changing the color or magical properties of registered pirate hats.",
        "fee": "20 gold + magical dye tax",
        "application": "Requires submitting before and after photos of the hat.",
        "funny_note": "Unauthorized fluorescent or glow-in-the-dark dyes are punishable by “hat probation.”"
    },
    {
        "name": "Unauthorized Singing Permit",
        "description": "Allows singing of unlicensed sea shanties aboard ships or in ports.",
        "fee": "5 gold per song + noise complaint waiver",
        "application": "Must submit lyrics and melody for review.",
        "funny_note": "Singing banned shanties may result in “silencing fines” or temporary gag orders."
    },
    {
        "name": "Parrot Ownership Permit",
        "description": "Registers parrots as official crew members or pets aboard vessels.",
        "fee": "10 gold + pet care inspection",
        "application": "Includes proof of parrot literacy in official languages.",
        "funny_note": "Unregistered parrots are subject to confiscation and forced to attend “behavioral reform sessions.”"
    }
]

@app.get("/")
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/laws")
async def laws(request: Request):
    return templates.TemplateResponse("laws.html", {"request": request, "laws": laws_data})

@app.get("/permits")
async def permits(request: Request):
    return templates.TemplateResponse("permits.html", {"request": request, "permits": permits_data})

@app.get("/permit")
async def permit(request: Request):
    return templates.TemplateResponse("permit.html", {"request": request})

@app.get("/documents")
async def documents(request: Request):
    return templates.TemplateResponse("documents.html", {"request": request, "documents": documents_data})

from fastapi import HTTPException

@app.get("/documents/{document_id}")
async def document_detail(request: Request, document_id: str):
    # Find document by id
    doc = next((d for d in documents_data if d["id"] == document_id), None)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")

    # Optional: convert ratification_date string to datetime if it's not already
    if doc.get("ratification_date") and isinstance(doc["ratification_date"], str):
        from datetime import datetime
        try:
            doc["ratification_date"] = datetime.fromisoformat(doc["ratification_date"])
        except ValueError:
            doc["ratification_date"] = None

    # Ensure required keys exist to prevent template errors
    doc.setdefault("parties", [])
    doc.setdefault("signatories", [])
    doc.setdefault("text", "No document text available.")

    return templates.TemplateResponse("document_detail.html", {"request": request, "document": doc})

@app.get("/laws/{law_id}")
async def law_detail(request: Request, law_id: str):
    law = next((l for l in laws_data if l["id"] == law_id), None)
    if law is None:
        raise HTTPException(status_code=404, detail="Law not found")
    return templates.TemplateResponse("law_detail.html", {"request": request, "law": law})

@app.get("/login")
async def login():
    discord_oauth_url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={DISCORD_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=identify guilds.members.read"
        f"&prompt=consent"
    )
    return RedirectResponse(discord_oauth_url)

@app.get("/health")
async def health_check():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
