import logging
import re
import os
import sqlite3
import asyncio
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest

# --- Load environment variables from .env file ---
load_dotenv()

# --- Configuration ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_USERID = os.getenv("ADMIN_USERID")
DB_FILE = "user_data.db"
UPI_NUMBER = "6372833479"
UPI_NAME = "Durgamadhav Pati"



# --- FLASK APP FOR RENDER HEALTH CHECK ---
app = Flask(__name__)

@app.route("/")
def home():
    return "Quiz Bot is running!"

# Function to start Flask in a separate thread
def start_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# --- Set up logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Database Functions ---

def init_db():
    """Initializes the database and creates/updates tables."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        # Create users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                is_bot_use BOOLEAN NOT NULL DEFAULT 0,
                plan_expiry_date DATETIME,
                assigned_username TEXT,
                assigned_password TEXT
            )
        """)
        # Create credentials table for the pool
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'available', -- available, in_use
                credential_expiry_date DATETIME
            )
        """)
        # Compatibility checks for old schemas
        try:
            cursor.execute("ALTER TABLE credentials ADD COLUMN credential_expiry_date DATETIME")
        except sqlite3.OperationalError:
            pass # Column already exists
        try:
            cursor.execute("ALTER TABLE users RENAME COLUMN chess_username TO assigned_username")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN assigned_password TEXT")
        except sqlite3.OperationalError:
            pass
        conn.commit()


def get_user(user_id: int):
    """Retrieves a user from the database."""
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return cursor.fetchone()

def get_credential(username: str):
    """Retrieves a specific credential from the pool."""
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM credentials WHERE username = ?", (username,))
        return cursor.fetchone()

def add_or_get_user(user_id: int):
    """Adds a new user if they don't exist, then returns their data."""
    user = get_user(user_id)
    if not user:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
            conn.commit()
        logger.info(f"New user created in DB with ID: {user_id}")
        return get_user(user_id)
    return user

def add_credential_to_pool(username, password, days):
    """Adds a new credential to the pool with an expiry date."""
    expiry_date = datetime.now(timezone.utc) + timedelta(days=days)
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO credentials (username, password, status, credential_expiry_date) VALUES (?, ?, 'available', ?)",
                (username, password, expiry_date)
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            logger.warning(f"Attempted to add duplicate username to credential pool: {username}")
            return False

def edit_credential_in_pool(username, new_password, new_days):
    """Edits an existing credential in the pool."""
    new_expiry_date = datetime.now(timezone.utc) + timedelta(days=new_days)
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        # Check if the credential exists first
        cursor.execute("SELECT id FROM credentials WHERE username = ?", (username,))
        if cursor.fetchone() is None:
            return False # Credential doesn't exist
        
        cursor.execute(
            "UPDATE credentials SET password = ?, credential_expiry_date = ? WHERE username = ?",
            (new_password, new_expiry_date, username)
        )
        conn.commit()
        return True

def get_available_credential(required_days: int):
    """Gets the soonest-expiring credential that can last for the required duration."""
    required_expiry_date = datetime.now(timezone.utc) + timedelta(days=required_days)
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM credentials 
            WHERE status = 'available' AND credential_expiry_date >= ? 
            ORDER BY credential_expiry_date ASC 
            LIMIT 1
            """,
            (required_expiry_date,)
        )
        return cursor.fetchone()

def get_all_available_credentials():
    """Gets all available, non-expired credentials from the pool."""
    now = datetime.now(timezone.utc)
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM credentials
            WHERE status = 'available' AND credential_expiry_date > ?
            ORDER BY credential_expiry_date ASC
            """,
            (now,)
        )
        return cursor.fetchall()

def get_all_used_credentials():
    """Gets all 'in_use' credentials and the user they are assigned to."""
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT c.username, c.credential_expiry_date, u.user_id 
            FROM credentials c
            JOIN users u ON c.username = u.assigned_username
            WHERE c.status = 'in_use'
            """
        )
        return cursor.fetchall()

def update_credential_status(username, status):
    """Updates the status of a credential."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE credentials SET status = ? WHERE username = ?", (status, username))
        conn.commit()

def assign_credential_to_user(user_id, cred):
    """Assigns a credential to a user."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET assigned_username = ?, assigned_password = ? WHERE user_id = ?", (cred['username'], cred['password'], user_id))
        conn.commit()
    update_credential_status(cred['username'], 'in_use')
    logger.info(f"Assigned credential {cred['username']} to user {user_id}")

def grant_user_access(user_id: int, days: int):
    """Grants a user plan access for a specific number of days."""
    expiry_date = datetime.now(timezone.utc) + timedelta(days=days)
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET is_bot_use = 1, plan_expiry_date = ? WHERE user_id = ?",
            (expiry_date, user_id)
        )
        conn.commit()
    logger.info(f"Granted plan access to user {user_id} for {days} days. Expires on {expiry_date.isoformat()}")

def revoke_user_access(user_id: int):
    """Revokes a user's plan access."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET is_bot_use = 0, plan_expiry_date = NULL WHERE user_id = ?", (user_id,))
        conn.commit()
    logger.info(f"Revoked access for user {user_id}")

def free_credential_from_user(user_id: int):
    """Frees a credential from a user, making it available again."""
    user = get_user(user_id)
    if not user or not user['assigned_username']:
        return None # User has no credential assigned
    
    username_to_free = user['assigned_username']
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET assigned_username = NULL, assigned_password = NULL WHERE user_id = ?", (user_id,))
        conn.commit()
    update_credential_status(username_to_free, 'available')
    logger.info(f"Freed credential {username_to_free} from user {user_id}")
    return username_to_free


# --- Conversation Handler States ---
CHOOSING_PLAN, AWAITING_PAYMENT_CONFIRM, AWAITING_SCREENSHOT = range(3)

# --- Helper Functions ---
def escape_markdown(text: str) -> str:
    """Escapes special characters for Telegram's MarkdownV2."""
    if not text: return ""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', str(text))

async def check_user_permission(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Checks if a user is permitted to use the bot's core feature."""
    user_id = update.effective_user.id
    user_record = add_or_get_user(user_id)
    return user_record['is_bot_use']

async def is_admin(user_id: int) -> bool:
    """Checks if the user is the admin."""
    return str(user_id) == ADMIN_USERID

# --- Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    logger.info(f"User {user.first_name} ({user.id}) started the bot.")
    add_or_get_user(user.id)
    escaped_name = escape_markdown(user.first_name)
    welcome_message = f"""
*Welcome to Chess Review Bot*, {escaped_name}\\! 💎

I Review your `chess\\.com` games right here in Telegram\\.

*To access this feature, you need an active plan\\. Choose one below to get started\\!*
    """
    
    keyboard = []
    
    default_plans = [
        {'duration': 14, 'price': 14, 'text': '2 Weeks'},
        {'duration': 7, 'price': 7, 'text': '1 Week'}
    ]
    dynamic_durations = [2, 3, 4, 5, 8, 9, 10, 11, 12, 13]

    for plan in default_plans:
        keyboard.append([InlineKeyboardButton(f"🛒 Buy {plan['text']} ({plan['duration']} Days) - ₹{plan['price']}", callback_data=f"buy_{plan['duration']}_days")])

    for duration in dynamic_durations:
        if get_available_credential(duration):
            price = duration
            keyboard.append([InlineKeyboardButton(f"🛒 Buy {duration} Days - ₹{price}", callback_data=f"buy_{duration}_days")])

    if keyboard:
        keyboard.append([InlineKeyboardButton("ℹ️ My Details", callback_data="my_details")])
        reply_markup = InlineKeyboardMarkup(keyboard)
    else:
        welcome_message += "\n\n*Sorry, there are currently no plans available\\. Please check back later or contact the admin\\.*"
        reply_markup = None


    message_to_reply = update.message or (update.callback_query.message if update.callback_query else None)
    if message_to_reply:
        if update.callback_query:
            await message_to_reply.edit_text(welcome_message, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await message_to_reply.reply_text(welcome_message, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    return CHOOSING_PLAN

async def handle_chess_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_user_permission(update, context):
        await update.message.reply_text(
            "❌ *Access Denied*\\.\n\nYou do not have permission to use this feature\\. Please purchase a plan by sending the /start command\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return
    message_text = update.message.text
    match = re.search(r"chess\.com/.*/(\d{9,})", message_text)
    if match:
        game_id = match.group(1)
        analysis_url = f"https://www.chess.com/analysis/game/live/{game_id}"
        keyboard = [[InlineKeyboardButton("🔬 Open Game Review", web_app=WebAppInfo(url=analysis_url))]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("✅ *Game link found\\!* Click the button below to start your review:", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    else:
        await update.message.reply_text("Please send a valid `chess\\.com` game link\\.", parse_mode=ParseMode.MARKDOWN_V2)

async def handle_buy_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    try:
        duration_str = query.data.split('_')[1]
        duration = int(duration_str)
    except (IndexError, ValueError):
        await query.edit_message_text("Invalid selection\\. Please try again\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END
    
    if duration == 7:
        price = 7
        text = "1 Week"
    elif duration == 14:
        price = 14
        text = "2 Weeks"
    else:
        price = duration
        text = f"{duration} Days"
        
    selected_plan = {"price": price, "duration": duration, "text": text}

    if not get_available_credential(duration):
        sent_msg = await query.edit_message_text("Sorry, this plan is currently out of stock\\. Please choose another plan\\.", parse_mode=ParseMode.MARKDOWN_V2)
        await asyncio.sleep(3)
        try:
            await sent_msg.delete()
        except BadRequest:
            pass 
        await start(update, context)
        return ConversationHandler.END

    context.user_data['plan'] = selected_plan
    escaped_upi_name = escape_markdown(UPI_NAME)
    payment_message = f"""
*You have selected the {selected_plan['text']} plan for ₹{selected_plan['price']}\\.*

Please pay the amount to the following UPI ID:
`{UPI_NUMBER}`
Name: *{escaped_upi_name}*

Once paid, please press the button below\\.
    """
    keyboard = [
        [InlineKeyboardButton("✅ I Have Paid", callback_data="paid")],
        [InlineKeyboardButton("❌ Cancel Order", callback_data="cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text=payment_message, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
    return AWAITING_PAYMENT_CONFIRM

async def handle_payment_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(text="Great\\! Please send me the screenshot of your payment for verification\\.", parse_mode=ParseMode.MARKDOWN_V2)
    return AWAITING_SCREENSHOT

async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    plan = context.user_data.get('plan')
    if not plan:
        await update.message.reply_text("Something went wrong\\. Please start again with /start\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    escaped_first_name = escape_markdown(user.first_name)
    escaped_last_name = escape_markdown(user.last_name or '')
    escaped_username = escape_markdown(user.username or 'N/A')
    
    plan_text = f"{plan['text']} ({plan['duration']} days) - ₹{plan['price']}"
    permit_command = f"/permitbotuse {user.id} {plan['duration']}"
    
    caption = (
        f"📸 *New Order Confirmation* 📸\n\n"
        f"*User:* {escaped_first_name} {escaped_last_name} (`{user.id}`)\n"
        f"*Username:* @{escaped_username}\n"
        f"*Plan:* {plan_text}"
    )
    try:
        await context.bot.send_photo(chat_id=ADMIN_USERID, photo=update.message.photo[-1].file_id, caption=caption, parse_mode=ParseMode.MARKDOWN_V2)
        await context.bot.send_message(chat_id=ADMIN_USERID, text=f"`{permit_command}`", parse_mode=ParseMode.MARKDOWN_V2)
    except BadRequest as e:
        logger.error(f"Markdown parse error sending to admin: {e}. Sending plain text.")
        plain_text_caption = caption.replace('*', '').replace('`', '')
        await context.bot.send_photo(chat_id=ADMIN_USERID, photo=update.message.photo[-1].file_id, caption=plain_text_caption)
        await context.bot.send_message(chat_id=ADMIN_USERID, text=permit_command)
    
    await update.message.reply_text("Thank you\\! Your order is being verified\\. You'll be notified once access is granted\\.", parse_mode=ParseMode.MARKDOWN_V2)
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    return await start(update, context)

async def my_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    base_message = update.message or (query.message if query else None)
    if not base_message: return CHOOSING_PLAN
    if query: await query.answer()

    user_id = update.effective_user.id
    user_record = get_user(user_id)
    
    username = escape_markdown(user_record['assigned_username'] if user_record and user_record['assigned_username'] else "Not set")
    password = escape_markdown(user_record['assigned_password'] if user_record and user_record['assigned_password'] else "Not set")
    
    expiry_date = user_record['plan_expiry_date'] if user_record and user_record['assigned_username'] else None
    
    if expiry_date:
        if isinstance(expiry_date, str):
            expiry_date = datetime.fromisoformat(expiry_date)
        expiry_date = expiry_date.replace(tzinfo=timezone.utc)
        local_tz = timezone(timedelta(hours=5, minutes=30)) # IST
        expiry_local_str = escape_markdown(expiry_date.astimezone(local_tz).strftime("%d %b %Y, %I:%M %p"))
        expiry_text = f"*Plan Expires:* `{expiry_local_str}`"
    else:
        expiry_text = "*Plan Expires:* `N/A`"

    details_message_text = f"""
*Your Details* ℹ️

*Chess\\.com Username:* `{username}`
*Chess\\.com Password:* `{password}`
{expiry_text}

_This message will disappear in 10 seconds\\._
    """
    
    sent_message = await base_message.reply_text(
        text=details_message_text,
        parse_mode=ParseMode.MARKDOWN_V2
    )

    await asyncio.sleep(10)

    try:
        await sent_message.delete()
        if update.message:
            await update.message.delete()
    except BadRequest as e:
        if "Message to delete not found" in str(e):
            logger.info("Message for /mydetails was already deleted by user.")
        else:
            logger.error(f"Error deleting /mydetails message: {e}")
    
    return CHOOSING_PLAN


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    
    user_commands = """
*User Commands* 🤖
`/start` \\- Start the bot & see plans
`/mydetails` \\- See your assigned credentials
`/help` \\- Show this help message
    """
    
    admin_commands = """
*Admin Commands* 👑
`/addcredential <user> <pass> <days>` \\- Add a new credential to the pool
`/editcredential <user> <new_pass> <days>` \\- Edit an existing credential
`/availablecreds` \\- See all available credentials
`/usedcreds` \\- See all assigned credentials
`/permitbotuse <id> <days>` \\- Grant a user access
`/restrictbotuse <id>` \\- Revoke a user's access
`/freecredential <id>` \\- Free a credential from a user
`/seedetails <id>` \\- See all details for a specific user
    """
    
    if await is_admin(user_id):
        help_text = user_commands + "\n" + admin_commands
    else:
        help_text = user_commands
        
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN_V2)


# --- Admin Commands ---
async def add_credential(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update.effective_user.id): return
    if len(context.args) != 3:
        await update.message.reply_text("Usage: `/addcredential <username> <password> <days>`", parse_mode=ParseMode.MARKDOWN_V2)
        return
    username, password, days_str = context.args
    try:
        days = int(days_str)
        safe_username = escape_markdown(username)
        if add_credential_to_pool(username, password, days):
            await update.message.reply_text(f"✅ Credential `{safe_username}` added to the pool\\. It will expire in {days} days\\.", parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await update.message.reply_text(f"❌ Credential `{safe_username}` already exists in the pool\\.", parse_mode=ParseMode.MARKDOWN_V2)
    except ValueError:
        await update.message.reply_text("Invalid number of days\\. Please provide an integer\\.", parse_mode=ParseMode.MARKDOWN_V2)

async def edit_credential(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update.effective_user.id): return
    if len(context.args) != 3:
        await update.message.reply_text("Usage: `/editcredential <username> <new_password> <new_days>`", parse_mode=ParseMode.MARKDOWN_V2)
        return
    username, new_password, days_str = context.args
    try:
        days = int(days_str)
        safe_username = escape_markdown(username)
        if edit_credential_in_pool(username, new_password, days):
            await update.message.reply_text(f"✅ Credential `{safe_username}` has been updated successfully\\.", parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await update.message.reply_text(f"❌ Credential `{safe_username}` not found in the pool\\.", parse_mode=ParseMode.MARKDOWN_V2)
    except ValueError:
        await update.message.reply_text("Invalid number of days\\. Please provide an integer\\.", parse_mode=ParseMode.MARKDOWN_V2)

async def see_available_credentials(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update.effective_user.id): return

    creds = get_all_available_credentials()
    if not creds:
        await update.message.reply_text("No available credentials in the pool\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    now = datetime.now(timezone.utc)
    message_lines = ["*Available Credentials* 🕵️\n"]

    for cred in creds:
        username = escape_markdown(cred['username'])
        expiry_date = cred['credential_expiry_date']

        if isinstance(expiry_date, str):
            expiry_date = datetime.fromisoformat(expiry_date)
        expiry_date = expiry_date.replace(tzinfo=timezone.utc)

        remaining_time = expiry_date - now
        days = remaining_time.days
        hours = remaining_time.seconds // 3600
        
        lifetime_str = f"{days} days, {hours} hours"
        
        message_lines.append(f"• `{username}` \\- Expires in: {lifetime_str}")

    await update.message.reply_text("\n".join(message_lines), parse_mode=ParseMode.MARKDOWN_V2)

async def see_used_credentials(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update.effective_user.id): return

    creds = get_all_used_credentials()
    if not creds:
        await update.message.reply_text("No credentials are currently in use\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    now = datetime.now(timezone.utc)
    message_lines = ["*Used Credentials* 🕵️\n"]

    for cred in creds:
        username = escape_markdown(cred['username'])
        user_id = escape_markdown(str(cred['user_id']))
        expiry_date = cred['credential_expiry_date']

        if isinstance(expiry_date, str):
            expiry_date = datetime.fromisoformat(expiry_date)
        expiry_date = expiry_date.replace(tzinfo=timezone.utc)

        remaining_time = expiry_date - now
        
        if remaining_time.total_seconds() < 0:
            lifetime_str = "Expired"
        else:
            days = remaining_time.days
            hours = remaining_time.seconds // 3600
            lifetime_str = f"{days} days, {hours} hours"
        
        message_lines.append(f"• `{username}` \\- Assigned to: `{user_id}` \\- Expires in: {lifetime_str}")

    await update.message.reply_text("\n".join(message_lines), parse_mode=ParseMode.MARKDOWN_V2)

async def run_cleanup_task_after_delay(context: ContextTypes.DEFAULT_TYPE, user_id: int, delay_seconds: int):
    """
    An asynchronous task that waits for a specified delay, then runs the cleanup.
    """
    logger.info(f"Starting background cleanup task for user {user_id}. Will run in {delay_seconds} seconds.")
    
    await asyncio.sleep(delay_seconds)
    
    logger.info(f"Executing scheduled cleanup for user_id: {user_id}")
    
    try:
        freed_username = free_credential_from_user(user_id)
        if freed_username:
            revoke_user_access(user_id)
            safe_freed_username = escape_markdown(freed_username)
            await context.bot.send_message(
                chat_id=ADMIN_USERID,
                text=f"🤖 *Automated Cleanup Complete* 🤖\n\nPlan for user `{user_id}` has expired\\. Credential `{safe_freed_username}` has been automatically freed and is now available in the pool\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            await context.bot.send_message(
                chat_id=user_id,
                text="Your plan has expired\\. Please use /start to purchase a new one\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        else:
            await context.bot.send_message(
                chat_id=ADMIN_USERID,
                text=f"🔔 *Plan Expired Notification* 🔔\n\nThe plan for user `{user_id}` has ended\\. Automatic cleanup was skipped as they had no credential assigned\\. Please check manually if needed\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
    except Exception as e:
        logger.error(f"Failed during scheduled cleanup for user {user_id}: {e}")

async def permit_bot_use(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update.effective_user.id): return
    try:
        if len(context.args) != 2:
            await update.message.reply_text("Usage: `/permitbotuse <user_id> <days>`", parse_mode=ParseMode.MARKDOWN_V2)
            return
        target_user_id = int(context.args[0])
        days_to_grant = int(context.args[1])
        if not get_user(target_user_id):
            await update.message.reply_text(f"User ID {target_user_id} not found in database\\.", parse_mode=ParseMode.MARKDOWN_V2)
            return

        available_cred = get_available_credential(days_to_grant)
        if not available_cred:
            await update.message.reply_text(f"❌ No available credentials with a sufficient lifetime for a {days_to_grant}\\-day plan\\! Use `/addcredential` to add one\\.", parse_mode=ParseMode.MARKDOWN_V2)
            return

        assign_credential_to_user(target_user_id, available_cred)
        grant_user_access(target_user_id, days_to_grant)
        
        seconds_to_wait = days_to_grant * 86400
        asyncio.create_task(
            run_cleanup_task_after_delay(context, target_user_id, seconds_to_wait)
        )
        
        safe_username = escape_markdown(available_cred['username'])
        await update.message.reply_text(
            f"✅ Access granted for user ID: {target_user_id} for {days_to_grant} days\\.\n"
            f"Assigned credential: `{safe_username}`\\.\n"
            f"🤖 *Automated cleanup task has been started in the background\\.*",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        
        await context.bot.send_message(
            chat_id=target_user_id, 
            text=f"🎉 Congratulations\\! Your access has been granted for *{days_to_grant} days*\\. You can now send me chess links\\. Use /mydetails to see your assigned login\\.", 
            parse_mode=ParseMode.MARKDOWN_V2
        )

    except (IndexError, ValueError):
        await update.message.reply_text("Usage: `/permitbotuse <user_id> <days>`\nExample: `/permitbotuse 1234567 14`", parse_mode=ParseMode.MARKDOWN_V2)


async def restrict_bot_use(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update.effective_user.id): return
    try:
        target_user_id = int(context.args[0])
        if get_user(target_user_id):
            revoke_user_access(target_user_id)
            await update.message.reply_text(f"❌ Access manually revoked for user ID: {target_user_id}\\.\n\nRemember to free their credential using `/freecredential {target_user_id}` if you want to add it back to the pool\\.", parse_mode=ParseMode.MARKDOWN_V2)
            await context.bot.send_message(chat_id=target_user_id, text="Your access to the bot has been revoked by the admin\\.", parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await update.message.reply_text(f"User ID {target_user_id} not found in database\\.", parse_mode=ParseMode.MARKDOWN_V2)
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /restrictbotuse <user_id>")

async def free_credential(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update.effective_user.id): return
    try:
        target_user_id = int(context.args[0])
        freed_username = free_credential_from_user(target_user_id)
        if freed_username:
            revoke_user_access(target_user_id)
            safe_freed_username = escape_markdown(freed_username)
            await update.message.reply_text(f"✅ Credential `{safe_freed_username}` has been freed from user {target_user_id} and is now available in the pool\\.", parse_mode=ParseMode.MARKDOWN_V2)
            await context.bot.send_message(chat_id=ADMIN_USERID, text=f"Plan for user `{target_user_id}` has ended\\.", parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await update.message.reply_text(f"User {target_user_id} had no credential assigned\\.", parse_mode=ParseMode.MARKDOWN_V2)
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /freecredential <user_id>")

async def see_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update.effective_user.id): return
    try:
        target_user_id = int(context.args[0])
        user_record = get_user(target_user_id)
        if not user_record:
            await update.message.reply_text(f"User ID {target_user_id} not found\\.", parse_mode=ParseMode.MARKDOWN_V2)
            return

        user_id_str = escape_markdown(str(user_record['user_id']))
        assigned_username = user_record['assigned_username']
        password = escape_markdown(user_record['assigned_password'] or "None")
        is_active = "Yes" if user_record['is_bot_use'] else "No"
        
        plan_expiry_date = user_record['plan_expiry_date']
        if plan_expiry_date:
            if isinstance(plan_expiry_date, str):
                plan_expiry_date = datetime.fromisoformat(plan_expiry_date)
            plan_expiry_date = plan_expiry_date.replace(tzinfo=timezone.utc)
            local_tz = timezone(timedelta(hours=5, minutes=30)) # IST
            plan_expiry_str = escape_markdown(plan_expiry_date.astimezone(local_tz).strftime("%d %b %Y, %I:%M %p"))
            plan_expiry_text = f"`{plan_expiry_str}`"
        else:
            plan_expiry_text = "`N/A`"
        
        cred_expiry_text = "`N/A`"
        if assigned_username:
            cred_record = get_credential(assigned_username)
            if cred_record and cred_record['credential_expiry_date']:
                cred_expiry_date = cred_record['credential_expiry_date']
                if isinstance(cred_expiry_date, str):
                    cred_expiry_date = datetime.fromisoformat(cred_expiry_date)
                cred_expiry_date = cred_expiry_date.replace(tzinfo=timezone.utc)
                local_tz = timezone(timedelta(hours=5, minutes=30)) # IST
                cred_expiry_str = escape_markdown(cred_expiry_date.astimezone(local_tz).strftime("%d %b %Y, %I:%M %p"))
                cred_expiry_text = f"`{cred_expiry_str}`"

        details_message = f"""
*Admin Details for User* 🕵️

*User ID:* `{user_id_str}`
*Plan Active:* {is_active}
*Plan Expires:* {plan_expiry_text}

*Assigned Username:* `{escape_markdown(assigned_username or "None")}`
*Assigned Password:* `{password}`
*Credential Expires:* {cred_expiry_text}
        """
        await update.message.reply_text(details_message, parse_mode=ParseMode.MARKDOWN_V2)

    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /seedetails <user_id>")


def main() -> None:
    """Start Flask in a thread and the bot using polling."""
    # Start Flask server in a background thread
    threading.Thread(target=start_flask, daemon=True).start()

    
    """The main function to start the bot."""
    if not BOT_TOKEN or not ADMIN_USERID:
        logger.critical("!!! ERROR: TELEGRAM_BOT_TOKEN or ADMIN_USERID not found in .env file. !!!")
        return

    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_PLAN: [
                CallbackQueryHandler(handle_buy_plan, pattern=r"^buy_"),
                CallbackQueryHandler(my_details, pattern="^my_details$")
            ],
            AWAITING_PAYMENT_CONFIRM: [CallbackQueryHandler(handle_payment_confirmation, pattern="^paid$"), CallbackQueryHandler(cancel_order, pattern="^cancel$")],
            AWAITING_SCREENSHOT: [MessageHandler(filters.PHOTO, handle_screenshot)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True
    )
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("mydetails", my_details))
    application.add_handler(CommandHandler("help", help_command))
    # Admin commands
    application.add_handler(CommandHandler("addcredential", add_credential))
    application.add_handler(CommandHandler("editcredential", edit_credential))
    application.add_handler(CommandHandler("availablecreds", see_available_credentials))
    application.add_handler(CommandHandler("usedcreds", see_used_credentials))
    application.add_handler(CommandHandler("permitbotuse", permit_bot_use))
    application.add_handler(CommandHandler("restrictbotuse", restrict_bot_use))
    application.add_handler(CommandHandler("freecredential", free_credential))
    application.add_handler(CommandHandler("seedetails", see_details))
    # Regular message handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chess_link))

    logger.info("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":

     main()


