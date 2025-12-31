const { Telegraf } = require("telegraf");
const fs = require("fs");

const bot = new Telegraf(process.env.BOT_TOKEN);

const TARGET = process.env.TARGET_CHANNEL_ID;
const LOG = process.env.LOG_CHANNEL_ID;
const OWNER = process.env.OWNER_ID;

let dailyPost = {};
let dailyDownload = {};
let welcomed = new Set();
let banned = {};

const resetDaily = () => {
  dailyPost = {};
  dailyDownload = {};
};
setInterval(resetDaily, 24 * 60 * 60 * 1000);

// ===== UTIL =====
const isBanned = (id) => banned[id] && banned[id] > Date.now();
const log = (ctx, gender, content) => {
  ctx.telegram.sendMessage(LOG,
    `📄 LOG POST
Nama: ${ctx.from.first_name}
Username: @${ctx.from.username || "-"}
ID: ${ctx.from.id}
Gender: ${gender}
Isi: ${content}`
  );
};

// ===== MEMBER POST =====
bot.on(["text", "photo", "video"], async (ctx) => {
  if (ctx.chat.type !== "private") return;
  if (isBanned(ctx.from.id)) return ctx.reply("⛔ Kamu sedang diban sementara.");

  const text = ctx.message.text || ctx.message.caption || "";
  const gender = text.includes("#pria") ? "Pria" :
                 text.includes("#wanita") ? "Wanita" : null;

  if (!gender) return ctx.reply("Wajib sertakan #pria atau #wanita");

  dailyPost[ctx.from.id] ??= { text: 0, media: 0 };

  // === TEXT ===
  if (ctx.message.text) {
    if (dailyPost[ctx.from.id].text >= 5)
      return ctx.reply("❌ Batas teks harian tercapai");

    dailyPost[ctx.from.id].text++;

    await ctx.telegram.sendMessage(TARGET, ctx.message.text, {
      disable_notification: true
    });

    log(ctx, gender, ctx.message.text);
  }

  // === MEDIA ===
  if (ctx.message.photo || ctx.message.video) {
    if (dailyPost[ctx.from.id].media >= 10)
      return ctx.reply("❌ Batas media harian tercapai");

    dailyPost[ctx.from.id].media++;

    await ctx.telegram.copyMessage(
      TARGET,
      ctx.chat.id,
      ctx.message.message_id,
      { disable_notification: true }
    );

    log(ctx, gender, "MEDIA");
  }
});

// ===== DOWNLOAD LIMIT =====
bot.hears(/https?:\/\//, (ctx) => {
  dailyDownload[ctx.from.id] ??= 0;
  if (dailyDownload[ctx.from.id] >= 2)
    return ctx.reply("❌ Limit download harian tercapai");

  dailyDownload[ctx.from.id]++;
  ctx.reply("🔽 Link diterima. (Downloader eksternal bisa ditambahkan)");
});

// ===== ANTI LINK GROUP =====
bot.on("message", async (ctx, next) => {
  if (ctx.chat.type === "private") return next();
  if (ctx.chat.type === "group" || ctx.chat.type === "supergroup") {
    if (ctx.message.text?.includes("http")) {
      const member = await ctx.getChatMember(ctx.from.id);
      if (member.status !== "administrator") {
        await ctx.deleteMessage();
        banned[ctx.from.id] = Date.now() + 60 * 60 * 1000;
      }
    }
  }
  next();
});

// ===== WELCOME =====
bot.on("new_chat_members", (ctx) => {
  ctx.message.new_chat_members.forEach((u) => {
    if (!welcomed.has(u.id)) {
      welcomed.add(u.id);
      ctx.reply(`👋 Selamat datang ${u.first_name}`);
    }
  });
});

// ===== ADMIN =====
bot.command("ban", (ctx) => {
  if (ctx.from.id != OWNER) return;
  const [_, id, jam] = ctx.message.text.split(" ");
  banned[id] = Date.now() + (jam || 1) * 60 * 60 * 1000;
  ctx.reply(`User ${id} diban`);
});

bot.command("unban", (ctx) => {
  if (ctx.from.id != OWNER) return;
  const id = ctx.message.text.split(" ")[1];
  delete banned[id];
  ctx.reply("Unban berhasil");
});

bot.command("kick", async (ctx) => {
  if (ctx.from.id != OWNER) return;
  const id = ctx.message.text.split(" ")[1];
  await ctx.kickChatMember(ctx.chat.id, id);
  ctx.reply("User dikick");
});

bot.launch();
console.log("Bot hidup.");


