```python
import os
import random
import time
from datetime import datetime, timedelta
from string import ascii_letters as letters

import httpx
import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)
from telegram.helpers import mention_html

from db.database import SessionMaker, engine
from db.model import Base, FormnStatus, MediaGroupMesssage, MessageMap, User

from . import (
    admin_group_id,
    admin_user_ids,
    app_name,
    bot_token,
    is_delete_topic_as_ban_forever,
    is_delete_user_messages,
    logger,
    welcome_message,
    disable_captcha,
    message_interval,
)
from .utils import delete_message_later

# Create tables (using SQLite, which cannot easily alter tables. If modified, deletion and recreation is required. Cannot merge)
Base.metadata.create_all(bind=engine)
db = SessionMaker()


# Callback for delayed sending of media group messages
async def _send_media_group_later(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    media_group_id = job.data
    _, from_chat_id, target_id, dir = job.name.split("_")

    # Find the corresponding media group messages in the database
    media_group_msgs = (
        db.query(MediaGroupMesssage)
        .filter(
            MediaGroupMesssage.media_group_id == media_group_id,
            MediaGroupMesssage.chat_id == from_chat_id,
        )
        .all()
    )
    chat = await context.bot.get_chat(target_id)
    if dir == "u2a":
        # Send to group
        u = db.query(User).filter(User.user_id == from_chat_id).first()
        message_thread_id = u.message_thread_id
        sents = await chat.send_copies(
            from_chat_id,
            [m.message_id for m in media_group_msgs],
            message_thread_id=message_thread_id,
        )
        for sent, msg in zip(sents, media_group_msgs):
            msg_map = MessageMap(
                user_chat_message_id=msg.message_id,
                group_chat_message_id=sent.message_id,
                user_id=u.user_id,
            )
            db.add(msg_map)
            db.commit()
    else:
        # Send to user
        sents = await chat.send_copies(
            from_chat_id, [m.message_id for m in media_group_msgs]
        )
        for sent, msg in zip(sents, media_group_msgs):
            msg_map = MessageMap(
                user_chat_message_id=sent.message_id,
                group_chat_message_id=msg.message_id,
                user_id=target_id,
            )
            db.add(msg_map)
            db.commit()


# Delayed sending of media group messages
async def send_media_group_later(
    delay: float,
    chat_id,
    target_id,
    media_group_id: int,
    dir,
    context: ContextTypes.DEFAULT_TYPE,
):
    name = f"sendmediagroup_{chat_id}_{target_id}_{dir}"
    context.job_queue.run_once(
        _send_media_group_later, delay, chat_id=chat_id, name=name, data=media_group_id
    )
    return name


def update_user_db(user: telegram.User):
    if db.query(User).filter(User.user_id == user.id).first():
        return
    u = User(
        user_id=user.id,
        first_name=user.first_name,
        last_name=user.last_name,
        username=user.username,
    )
    db.add(u)
    db.commit()


async def send_contact_card(
    chat_id, message_thread_id, user: User, update: Update, context: ContextTypes
):
    buttons = []
    buttons.append(
        [
            InlineKeyboardButton(
                f"{'🏆 Premium Member' if user.is_premium else '✈️ Regular Member' }",
                url=f"https://github.com/MiHaKun/Telegram-interactive-bot",
            )
        ]
    )
    if user.username:
        buttons.append(
            [InlineKeyboardButton("👤 Direct Contact", url=f"https://t.me/{user.username}")]
        )

    user_photo = await context.bot.get_user_profile_photos(user.id)

    if user_photo.total_count:
        pic = user_photo.photos[0][-1].file_id
        await context.bot.send_photo(
            chat_id,
            photo=pic,
            caption=f"👤 {mention_html(user.id, user.first_name)}\n\n📱 {user.id}\n\n🔗 @{user.username if user.username else 'None'}",
            message_thread_id=message_thread_id,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="HTML",
        )
    else:
        await context.bot.send_contact(
            chat_id,
            phone_number="11111",
            first_name=user.first_name,
            last_name=user.last_name,
            message_thread_id=message_thread_id,
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    update_user_db(user)
    # check whether is admin
    if user.id in admin_user_ids:
        logger.info(f"{user.first_name}({user.id}) is admin")
        try:
            bg = await context.bot.get_chat(admin_group_id)
            if bg.type == "supergroup" or bg.type == "group":
                logger.info(f"admin group is {bg.title}")
        except Exception as e:
            logger.error(f"admin group error {e}")
            await update.message.reply_html(
                f"⚠️⚠️ Backend admin group configuration error, please check settings. ⚠️⚠️\nYou need to ensure that the bot @{context.bot.username} has been invited to the admin group and granted administrator privileges.\nError details: {e}\n"
            )
            return ConversationHandler.END
        await update.message.reply_html(
            f"Hello Administrator {user.first_name}({user.id})\n\nWelcome to the {app_name} bot.\n\nYour configuration is correct. You can use the bot in the group <b> {bg.title} </b>."
        )
    else:
        await update.message.reply_html(
            f"{mention_html(user.id, user.full_name)}:\n\n{welcome_message}"
        )


async def check_human(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if context.user_data.get("is_human", False) == False:
        if context.user_data.get("is_human_error_time", 0) > time.time() - 120:
            # Muted for 2 minutes
            await update.message.reply_html("You have been muted, please try again later.")
            return False
        file_name = random.choice(os.listdir("./assets/imgs"))
        code = file_name.replace("image_", "").replace(".png", "")
        file = f"./assets/imgs/{file_name}"
        codes = ["".join(random.sample(letters, 5)) for _ in range(0, 7)]
        codes.append(code)
        random.shuffle(codes)

        photo = context.bot_data.get(f"image|{code}")
        if not photo:
            # Not sent before, use built-in image
            photo = file
        buttons = [
            InlineKeyboardButton(x, callback_data=f"vcode_{x}_{user.id}") for x in codes
        ]
        button_matrix = [buttons[i : i + 4] for i in range(0, len(buttons), 4)]
        sent = await update.message.reply_photo(
            photo,
            f"{mention_html(user.id, user.first_name)} Please select the text shown in the image. Incorrect answers will prevent you from contacting support.",
            reply_markup=InlineKeyboardMarkup(button_matrix),
            parse_mode="HTML",
        )
        # Store the sent image
        biggest_photo = sorted(sent.photo, key=lambda x: x.file_size, reverse=True)[0]
        context.bot_data[f"image|{code}"] = biggest_photo.file_id
        context.user_data["vcode"] = code
        await delete_message_later(60, sent.chat.id, sent.message_id, context)
        return False
    return True


async def callback_query_vcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    code = query.data.split("_")[1]
    user_id = query.data.split("_")[2]
    if user_id == str(user.id):
        # Correct person clicked
        if code == context.user_data.get("vcode"):
            # Valid click
            await query.answer(f"Correct, welcome.")
            sent = await context.bot.send_message(
                update.effective_chat.id,
                f"{mention_html(user.id, user.first_name)}, welcome.",
                parse_mode="HTML",
            )
            context.user_data["is_human"] = True
        else:
            await query.answer(f"~Incorrect~, muted for 2 minutes")
            context.user_data["is_human_error_time"] = time.time()
    await query.message.delete()


async def forwarding_message_u2a(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not disable_captcha:
        if not await check_human(update, context):
            return
    if message_interval:
        if context.user_data.get("last_message_time", 0) > time.time() - message_interval:
            await update.message.reply_html("Please do not send messages too frequently.")
            return
        context.user_data["last_message_time"] = time.time()
    user = update.effective_user
    update_user_db(user)
    chat_id = admin_group_id
    attachment = update.message.effective_attachment
    # await update.message.forward(chat_id)
    u = db.query(User).filter(User.user_id == user.id).first()
    message_thread_id = u.message_thread_id
    if (
        f := db.query(FormnStatus)
        .filter(FormnStatus.message_thread_id == message_thread_id)
        .first()
    ):
        if f.status == "closed":
            await update.message.reply_html(
                "The support agent has closed the conversation. If you need to contact them, please use other means to ask support to reopen the conversation with you."
            )
            return
    if not message_thread_id:
        formn = await context.bot.create_forum_topic(
            chat_id,
            name=f"{user.full_name}|{user.id}",
        )
        message_thread_id = formn.message_thread_id
        u.message_thread_id = message_thread_id
        await context.bot.send_message(
            chat_id,
            f"New user {mention_html(user.id, user.full_name)} has started a new conversation.",
            message_thread_id=message_thread_id,
            parse_mode="HTML",
        )
        await send_contact_card(chat_id, message_thread_id, user, update, context)
        db.add(u)
        db.commit()

    # Build send parameters
    params = {"message_thread_id": message_thread_id}
    if update.message.reply_to_message:
        # User quoted a message. We need to find the message ID in the group
        reply_in_user_chat = update.message.reply_to_message.message_id
        if (
            msg_map := db.query(MessageMap)
            .filter(MessageMap.user_chat_message_id == reply_in_user_chat)
            .first()
        ):
            params["reply_to_message_id"] = msg_map.group_chat_message_id
    try:
        if update.message.media_group_id:
            msg = MediaGroupMesssage(
                chat_id=update.message.chat.id,
                message_id=update.message.message_id,
                media_group_id=update.message.media_group_id,
                is_header=False,
                caption_html=update.message.caption_html,
            )
            db.add(msg)
            db.commit()
            if update.message.media_group_id != context.user_data.get(
                "current_media_group_id", 0
            ):
                context.user_data["current_media_group_id"] = (
                    update.message.media_group_id
                )
                await send_media_group_later(
                    5, user.id, chat_id, update.message.media_group_id, "u2a", context
                )
            return
        else:
            chat = await context.bot.get_chat(chat_id)
            sent_msg = await chat.send_copy(
                update.effective_chat.id, update.message.id, **params
            )

        msg_map = MessageMap(
            user_chat_message_id=update.message.id,
            group_chat_message_id=sent_msg.message_id,
            user_id=user.id,
        )
        db.add(msg_map)
        db.commit()

    except BadRequest as e:
        if is_delete_topic_as_ban_forever:
            await update.message.reply_html(
                f"Send failed, your conversation has been deleted by support. Please contact support to reopen the conversation."
            )
        else:
            u.message_thread_id = 0
            db.add(u)
            db.commit()
            await update.message.reply_html(
                f"Send failed, your conversation has been deleted by support. Please send another message to reactivate the conversation."
            )
    except Exception as e:
        await update.message.reply_html(
            f"Send failed: {e}\n"
        )


async def forwarding_message_a2u(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_db(update.effective_user)
    message_thread_id = update.message.message_thread_id
    if not message_thread_id:
        # general message, ignore
        return
    user_id = 0
    if u := db.query(User).filter(User.message_thread_id == message_thread_id).first():
        user_id = u.user_id
    if not user_id:
        logger.debug(update.message)
        return
    if update.message.forum_topic_created:
        f = FormnStatus(
            message_thread_id=update.message.message_thread_id, status="opened"
        )
        db.add(f)
        db.commit()
        return
    if update.message.forum_topic_closed:
        await context.bot.send_message(
            user_id, "The conversation has ended. The other party has closed the conversation. Your messages will be ignored."
        )
        if (
            f := db.query(FormnStatus)
            .filter(FormnStatus.message_thread_id == update.message.message_thread_id)
            .first()
        ):
            f.status = "closed"
            db.add(f)
            db.commit()
        return
    if update.message.forum_topic_reopened:
        await context.bot.send_message(user_id, "The other party has reopened the conversation. You can continue chatting now.")
        if (
            f := db.query(FormnStatus)
            .filter(FormnStatus.message_thread_id == update.message.message_thread_id)
            .first()
        ):
            f.status = "opened"
            db.add(f)
            db.commit()
        return
    if (
        f := db.query(FormnStatus)
        .filter(FormnStatus.message_thread_id == message_thread_id)
        .first()
    ):
        if f.status == "closed":
            await update.message.reply_html(
                "The conversation has ended. To contact the other party, you need to reopen the conversation."
            )
            return
    chat_id = user_id
    # Build send parameters
    params = {}
    if update.message.reply_to_message:
        # In the group, support replied to a message. We need to find the message ID in the user chat
        reply_in_admin = update.message.reply_to_message.message_id
        if (
            msg_map := db.query(MessageMap)
            .filter(MessageMap.group_chat_message_id == reply_in_admin)
            .first()
        ):
            params["reply_to_message_id"] = msg_map.user_chat_message_id
    try:
        if update.message.media_group_id:
            msg = MediaGroupMesssage(
                chat_id=update.message.chat.id,
                message_id=update.message.message_id,
                media_group_id=update.message.media_group_id,
                is_header=False,
                caption_html=update.message.caption_html,
            )
            db.add(msg)
            db.commit()
            if update.message.media_group_id != context.application.user_data[
                user_id
            ].get("current_media_group_id", 0):
                context.application.user_data[user_id][
                    "current_media_group_id"
                ] = update.message.media_group_id
                await send_media_group_later(
                    5,
                    update.effective_chat.id,
                    user_id,
                    update.message.media_group_id,
                    "a2u",
                    context,
                )
            return
        else:
            chat = await context.bot.get_chat(chat_id)
            sent_msg = await chat.send_copy(
                update.effective_chat.id, update.message.id, **params
            )
        msg_map = MessageMap(
            group_chat_message_id=update.message.id,
            user_chat_message_id=sent_msg.message_id,
            user_id=user_id,
        )
        db.add(msg_map)
        db.commit()

    except Exception as e:
        await update.message.reply_html(
            f"Send failed: {e}\n"
        )


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user.id in admin_user_ids:
        await update.message.reply_html("You do not have permission to perform this operation.")
        return
    await context.bot.delete_forum_topic(
        update.effective_chat.id, update.message.message_thread_id
    )
    if not is_delete_user_messages:
        return
    if (
        target_user := db.query(User)
        .filter(User.message_thread_id == update.message.message_thread_id)
        .first()
    ):
        all_messages_in_user_chat = (
            db.query(MessageMap).filter(MessageMap.user_id == target_user.user_id).all()
        )
        await context.bot.delete_messages(
            target_user.user_id,
            [msg.user_chat_message_id for msg in all_messages_in_user_chat],
        )


async def _broadcast(context: ContextTypes.DEFAULT_TYPE):
    users = db.query(User).all()
    msg_id, chat_id = context.job.data.split("_")
    success = 0
    failed = 0
    for u in users:
        try:
            chat = await context.bot.get_chat(u.user_id)
            await chat.send_copy(chat_id, msg_id)
            success += 1
        except Exception as e:
            failed += 1


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user.id in admin_user_ids:
        await update.message.reply_html("You do not have permission to perform this operation.")
        return

    if not update.message.reply_to_message:
        await update.message.reply_html(
            "This command requires replying to a message. The replied message will be broadcast."
        )
        return

    context.job_queue.run_once(
        _broadcast,
        0,
        data=f"{update.message.reply_to_message.id}_{update.effective_chat.id}",
    )


async def error_in_send_media_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "Invalid message type. Exiting media group send. Subsequent messages will be forwarded directly."
    )
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the developer."""
    # Log the error before we do anything else, so we can see it even if something breaks.
    logger.error(f"Exception while handling an update: {context.error} ")
    logger.debug(f"Exception detail is :", exc_info=context.error)


if __name__ == "__main__":
    pickle_persistence = PicklePersistence(filepath=f"./assets/{app_name}.pickle")
    application = (
        ApplicationBuilder()
        .token(bot_token)
        .persistence(persistence=pickle_persistence)
        .build()
    )

    application.add_handler(CommandHandler("start", start, filters.ChatType.PRIVATE))

    application.add_handler(
        MessageHandler(
            ~filters.COMMAND & filters.ChatType.PRIVATE, forwarding_message_u2a
        )
    )
    application.add_handler(
        MessageHandler(
            ~filters.COMMAND & filters.Chat([admin_group_id]), forwarding_message_a2u
        )
    )
    application.add_handler(
        CommandHandler("clear", clear, filters.Chat([admin_group_id]))
    )
    application.add_handler(
        CommandHandler("broadcast", broadcast, filters.Chat([admin_group_id]))
    )
    application.add_handler(
        CallbackQueryHandler(callback_query_vcode, pattern="^vcode_")
    )
    application.add_error_handler(error_handler)
    application.run_polling()
```
