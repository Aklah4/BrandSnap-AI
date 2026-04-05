import json
import os
from functools import wraps

import requests
import cloudinary
import cloudinary.uploader
from flask import Flask, render_template, request, redirect, url_for, session, flash
from pymongo import MongoClient
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import secrets
import anthropic
import openai

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY')

_is_production = os.environ.get('RENDER') is not None  # Render sets this automatically
app.config['SESSION_COOKIE_SECURE']   = _is_production  # HTTPS-only cookie on Render, relaxed locally
app.config['SESSION_COOKIE_HTTPONLY'] = True            # block JS access to session cookie
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'           # CSRF protection


MONGO_URI         = os.environ.get('MONGO_URI')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
OPENAI_API_KEY    = os.environ.get('OPENAI_API_KEY')

cloudinary.config(cloudinary_url=os.environ.get('CLOUDINARY_URL'))

mongo_client = MongoClient(MONGO_URI)
db      = mongo_client["project1"]
users   = db["users"]
profile = db["profile"]
posts   = db["posts"]

# Flask-Mail configuration
app.config['MAIL_SERVER']         = 'smtp.gmail.com'
app.config['MAIL_PORT']           = 587
app.config['MAIL_USE_TLS']        = True
app.config['MAIL_USERNAME']       = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD']       = os.environ.get('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_USERNAME')

mail = Mail(app)


# ── Auth decorator ────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('email'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


# ── Root ──────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    if session.get('email'):
        return redirect(url_for('dashboard'))
    return render_template('index.html')


# ── Auth ──────────────────────────────────────────────────────────────────────
@app.route('/signup', methods=['GET', 'POST'])
def signup():

    if request.method == 'POST':
        firstname        = request.form.get('fname')
        lastname         = request.form.get('lname')
        email            = request.form.get('email')
        password         = request.form.get('password')
        confirm_password = request.form.get('confirm')

        if password != confirm_password:
            return render_template('signup.html', error="Passwords do not match")

        if users.find_one({"email": email}):
            return render_template('signup.html', error="An account with that email already exists")

        code = str(secrets.randbelow(900000) + 100000)

        users.insert_one({
            "firstname":         firstname,
            "lastname":          lastname,
            "email":             email,
            "password":          generate_password_hash(password),
            "verification_code": code,
            "verified":          False
        })

        session['email'] = email

        msg = Message(subject="Verify your email", recipients=[email])
        msg.body = f"Hi {firstname}, your verification code is: {code}"
        mail.send(msg)

        return redirect(url_for('verify'))

    return render_template('signup.html')


@app.route('/login', methods=['GET', 'POST'])
def login():

    if request.method == 'POST':
        email    = request.form.get('email')
        password = request.form.get('password')

        user = users.find_one({"email": email})

        if not user or not check_password_hash(user['password'], password):
            return render_template('login.html', error="Invalid email or password")

        if not user.get('verified'):
            session['email'] = email
            return redirect(url_for('verify'))

        session['email'] = email
        return redirect(url_for('dashboard'))

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/verify', methods=['GET', 'POST'])
def verify():

    if request.method == 'POST':
        submitted_code = request.form.get('code')
        email          = session.get('email')

        if not email:
            return redirect(url_for('login'))

        user = users.find_one({"email": email})

        if not user:
            return redirect(url_for('signup'))

        if user["verification_code"] == submitted_code:
            users.update_one({"email": email}, {"$set": {"verified": True}})
            return redirect(url_for('login'))
        else:
            return render_template('verify.html', error="Invalid code, please try again")

    return render_template('verify.html')


# ── App ───────────────────────────────────────────────────────────────────────
@app.route('/profile_setup', methods=['GET', 'POST'])
@login_required
def profile_setup():

    if request.method == 'POST':
        email           = session.get('email')
        business_name   = request.form.get('business_name')
        industry        = request.form.get('industry')
        tone            = request.form.get('tone')
        target_audience = request.form.get('target_audience')

        profile.update_one(
            {"email": email},
            {"$set": {
                "email":           email,
                "business_name":   business_name,
                "industry":        industry,
                "tone":            tone,
                "target_audience": target_audience
            }},
            upsert=True
        )

        return redirect(url_for('dashboard'))

    return render_template('profile_setup.html')


@app.route('/dashboard')
@login_required
def dashboard():
    email         = session.get('email')
    user_profile  = profile.find_one({"email": email})
    business_name = user_profile.get('business_name', '') if user_profile else ''
    user_posts    = list(posts.find({"email": email}))
    return render_template('dashboard.html', posts=user_posts, business_name=business_name)


@app.route('/generate', methods=['POST'])
@login_required
def generate():
    email        = session.get('email')
    user_profile = profile.find_one({"email": email})

    if not user_profile:
        return redirect(url_for('profile_setup'))

    business_name   = user_profile.get('business_name', '')
    industry        = user_profile.get('industry', '')
    tone            = user_profile.get('tone', '')
    target_audience = user_profile.get('target_audience', '')

    # ── 1. Generate captions, hashtags, and image prompts with Claude ──
    claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""You are a social media content creator.

Create 2 Instagram posts for a {industry} business called "{business_name}".
Target audience: {target_audience}
Tone: {tone}

Return ONLY a valid JSON array (no markdown, no explanation) with exactly 2 objects.
Each object must have these exact keys:
- "caption": 2-3 sentence post caption
- "hashtags": array of exactly 5 hashtag strings, each starting with #
- "image_prompt": a vivid, detailed prompt for AI image generation that suits the post (no text in the image)
"""

    try:
        claude_response = claude.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}]
        )
        raw_text = next(b.text for b in claude_response.content if b.type == "text")
        # Strip markdown code fences if present
        stripped = raw_text.strip()
        if stripped.startswith("```"):
            stripped = stripped.split("\n", 1)[-1]
            stripped = stripped.rsplit("```", 1)[0]
        generated_posts = json.loads(stripped.strip())
    except Exception as e:
        import traceback
        print(f"Claude generation error: {e}")
        traceback.print_exc()
        return render_template('dashboard.html',
                               posts=list(posts.find({"email": email})),
                               business_name=business_name,
                               error="Post generation failed. Please try again.")

    # ── 2. Generate one image per post with DALL-E 3 ──
    oai = openai.OpenAI(api_key=OPENAI_API_KEY)

    # Build the full list of new posts BEFORE touching the database.
    # This way the user never loses their old posts if something fails mid-way.
    new_posts = []
    for post_data in generated_posts:
        image_url = None
        try:
            img_response = oai.images.generate(
                model="dall-e-3",
                prompt=(
                    f"A high-quality, realistic Instagram post image for '{business_name}', a {industry} business. "
                    f"{post_data['image_prompt']} "
                    f"The brand name '{business_name}' is incorporated naturally into the scene — for example as signage, packaging, a logo on a product, an embossed label, or text on a surface. "
                    "Optionally include one or two short words or a phrase creatively placed in the composition (e.g. on a chalkboard, a tag, a storefront, an overlay). "
                    "Photorealistic, professional photography lighting, shallow depth of field, editorial quality."
                ),
                size="1024x1024",
                n=1
            )
            temp_url = img_response.data[0].url
            upload_result = cloudinary.uploader.upload(temp_url, folder="insta_posta")
            image_url = upload_result["secure_url"]
        except Exception as e:
            print(f"Image generation error: {e}")

        new_posts.append({
            "email":     email,
            "caption":   post_data["caption"],
            "hashtags":  post_data["hashtags"],
            "image_url": image_url
        })

    # All posts ready — now swap old for new atomically
    posts.delete_many({"email": email})
    posts.insert_many(new_posts)

    return redirect(url_for('dashboard'))


# ── Flyer Generator ───────────────────────────────────────────────────────────
_FLYER_STYLES = [
    (
        "App/SaaS Promo Card",
        "soft pastel background (mint, lavender, or peach — match the brand tone), "
        "bold dark serif brand name large at top center, short tagline below it, "
        "hero product or phone mockup centered in the middle floating on a warm circular color blob, "
        "three evenly-spaced feature cards in the lower third — cream/off-white rounded rectangles, "
        "each card contains one flat-style icon above a short 2-3 word feature label, "
        "a bold closing line at the very bottom center, "
        "clean uncluttered portrait orientation, professional SaaS marketing aesthetic"
    ),
    (
        "Minimalist Product Ad",
        "white space dominant, clean layout, single product hero shot centered, "
        "muted tones, brand name in clean sans-serif at top, generous padding, "
        "no clutter, premium feel"
    ),
    (
        "Bold Modern Streetwear",
        "high contrast colors, oversized bold typography overlapping a lifestyle image, "
        "sticker-style call-to-action badge, dynamic diagonal composition, "
        "urban energetic feel"
    ),
    (
        "Corporate Split-Screen",
        "left half: solid brand color block with brand name in large white text, "
        "right half: blurred lifestyle or product photo, "
        "clean horizontal dividing line, professional and trustworthy tone"
    ),
    (
        "Classic Promotional Flyer",
        "traditional flyer structure: large bold header at top, sub-header below, "
        "supporting body text in the middle, clear designated footer strip for promo details, "
        "high readability, print-ready layout"
    ),
]

_oai_client = openai.OpenAI()


@app.route('/flyer-generator')
@login_required
def flyer_generator():
    return render_template('generate.html')


@app.route('/generate-flyers', methods=['POST'])
@login_required
def generate_flyers():
    brand    = request.form.get('brand_name', '').strip()
    audience = request.form.get('target_audience', '').strip()
    details  = request.form.get('business_details', '').strip()

    if not brand:
        flash('Brand name is required.', 'error')
        return redirect(url_for('flyer_generator'))

    flyers = []

    for style_name, style_desc in _FLYER_STYLES:
        refined_prompt = (
            f"A professional {style_name} advertisement poster for a business named '{brand}'. "
            f"Design style: {style_desc}. "
            f"Target audience: {audience}. "
            f"Additional context: {details}. "
            f"The business name '{brand}' must appear prominently, perfectly spelled, "
            f"and clearly legible. Ultra high-end graphic design, 4K quality, print-ready. "
            f"Do not include any extra text besides the brand name and short feature labels."
        )
        try:
            response = _oai_client.images.generate(
                model="dall-e-3",
                prompt=refined_prompt,
                n=1,
                size="1024x1024",
                quality="standard"
            )
            flyers.append({"style": style_name, "url": response.data[0].url, "error": None})
        except Exception as e:
            print(f"Flyer generation error ({style_name}): {e}")
            flyers.append({"style": style_name, "url": None, "error": str(e)})

    generated = len([f for f in flyers if f["url"]])
    total     = len(_FLYER_STYLES)

    return render_template('results.html', flyers=flyers, brand=brand, generated=generated, total=total)


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=False)
