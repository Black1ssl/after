const express = require("express");
const { Telegraf } = require("telegraf");

const bot = new Telegraf(process.env.BOT_TOKEN);
const app = express();

const TARGET = process.env.TARGET_CHANNEL_ID;
const LOG = process.env.LOG_CHANNEL_ID;
const OWNER = Number(process.env.OWNER_ID);
const DOMAIN = process.env.WEBHOOK_DOMAIN;
const PORT = process.env.PORT || 3000;

app.use(express.json());

// ====== DATA ======
let dailyPost = {};
let dailyDownload = {};
let welcomed = new Set();
let banned = {};

setInterval(() => {
  dailyPost = {};
  dailyDownload = {};
}, 24 * 60 * 60 * 1000);

// ====== UTIL ======
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

// ================= MEMBER POST =================
bot.on(["text", "photo", "video"], async (ctx) => {
  if (ctx.chat.type !== "private") return;
  if (isBanned(ctx.from.id)) return ctx.reply("⛔ Kamu sedang diban sementara.");

  const text = ctx.message.text || ctx.message.caption || "";
  const gender = text.includes("#pria") ? "Pria" :
                 text.includes("#wanita") ? "Wanita" : null;

  if (!gender) return ctx.reply("Wajib sertakan #pria atau #wanita");

  dailyPost[ctx.from.id] ??= { text: 0, media: 0 };

  if (ctx.message.text) {
    if (dailyPost[ctx.from.id].text >= 5)
      return ctx.reply("❌ Batas teks harian tercapai");

    dailyPost[ctx.from.id].text++;
    await ctx.telegram.sendMessage(TARGET, ctx.message.text, {
      disable_notification: true
    });
    log(ctx, gender, ctx.message.text);
  }

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

// ====== WEBHOOK ENDPOINT ======
app.post("/", (req, res) => {
  bot.handleUpdate(req.body);
  res.sendStatus(200);
});

// ====== START SERVER ======
app.listen(PORT, async () => {
  await bot.telegram.setWebhook(DOMAIN);
  console.log("Bot hidup (WEBHOOK EXPRESS MODE)");
});
