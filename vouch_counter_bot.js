require('dotenv').config();
const CLIENT_ID = process.env.CLIENT_ID;
const GUILD_ID = process.env.GUILD_ID;
const TARGET_CHANNEL_ID = '1108034288986898458'; // Channel where images are posted
const NOTIFICATION_CHANNEL_ID = '1377459975089295392'; // Channel where notifications are sent
const { Client, GatewayIntentBits, REST, Routes, SlashCommandBuilder, PermissionFlagsBits, MessageFlags, ActivityType, ActionRowBuilder, ButtonBuilder, ButtonStyle, AttachmentBuilder } = require('discord.js');

// Define slash command for vouch overrides
const setvouchesCommand = new SlashCommandBuilder()
  .setName('setvouches')
  .setDescription('Override a user\'s vouches')
  .addUserOption(option =>
    option.setName('user')
      .setDescription('The user to set vouches for')
      .setRequired(true))
  .addIntegerOption(option =>
    option.setName('vouches')
      .setDescription('Number of vouches to set')
      .setRequired(true))
  .setDefaultMemberPermissions(PermissionFlagsBits.Administrator);

// Define slash command for checking user's vouches
const checkvouchesCommand = new SlashCommandBuilder()
  .setName('checkvouches')
  .setDescription('Show your current vouch total')
  .addUserOption(option =>
    option.setName('user')
      .setDescription('Optional: user to check vouches for')
      .setRequired(false));

// Define slash command for showing the leaderboard
const leaderboardCommand = new SlashCommandBuilder()
  .setName('leaderboard')
  .setDescription('Show top vouch earners')
  .addIntegerOption(option =>
    option.setName('limit')
      .setDescription('Number of users to display (default 10)')
      .setRequired(false));

// Define slash command for backfilling vouches from message history
const backfillCommand = new SlashCommandBuilder()
  .setName('backfill')
  .setDescription('Backfill vouches from existing messages in the channel')
  .setDefaultMemberPermissions(PermissionFlagsBits.ManageGuild);

// Define slash command for clearing vouches
const clearvouchesCommand = new SlashCommandBuilder()
  .setName('clearvouches')
  .setDescription('Clear vouches for a user or all users')
  .addUserOption(option =>
    option.setName('user')
      .setDescription('Optional: user to clear vouches for')
      .setRequired(false))
  .setDefaultMemberPermissions(PermissionFlagsBits.Administrator);

// Function to traverse channel history and increment vouches for image attachments
async function backfillChannelVouches(channel) {
  let lastId = null;
  let processed = 0;
  while (true) {
    const options = { limit: 100 };
    if (lastId) options.before = lastId;
    const batch = await channel.messages.fetch(options);
    if (!batch.size) break;
    for (const msg of batch.values()) {
      if (msg.attachments.some(att => att.contentType?.startsWith('image/'))) {
        const uid = msg.author.id;
        const cur = (await db.get(`vouches.${uid}`)) || 0;
        await db.set(`vouches.${uid}`, cur + 1);
      }
    }
    processed += batch.size;
    lastId = batch.last().id;
    // avoid rate limits
    await new Promise(r => setTimeout(r, 500));
  }
  return processed;
}

const fs = require('fs');
const path = require('path');

// Simple JSON file database
const dbPath = path.join(__dirname, 'vouches.json');

const db = {
  async get(key) {
    try {
      const data = JSON.parse(fs.readFileSync(dbPath, 'utf8'));
      return key.includes('.') ? this._getNestedValue(data, key) : data[key];
    } catch {
      return null;
    }
  },
  
  async set(key, value) {
    let data = {};
    try {
      data = JSON.parse(fs.readFileSync(dbPath, 'utf8'));
    } catch {
      data = {};
    }
    
    if (key.includes('.')) {
      this._setNestedValue(data, key, value);
    } else {
      data[key] = value;
    }
    
    fs.writeFileSync(dbPath, JSON.stringify(data, null, 2));
  },
  
  _getNestedValue(obj, key) {
    return key.split('.').reduce((o, k) => o && o[k], obj);
  },
  
  _setNestedValue(obj, key, value) {
    const keys = key.split('.');
    const lastKey = keys.pop();
    const target = keys.reduce((o, k) => o[k] = o[k] || {}, obj);
    target[lastKey] = value;
  }
};

const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.MessageContent
  ]
});

client.on('ready', async () => {
  // Register slash commands if IDs are set
  if (CLIENT_ID && GUILD_ID) {
    const rest = new REST({ version: '10' }).setToken(process.env.DISCORD_TOKEN);
    await rest.put(
      Routes.applicationGuildCommands(CLIENT_ID, GUILD_ID),
      { body: [setvouchesCommand.toJSON(), checkvouchesCommand.toJSON(), leaderboardCommand.toJSON(), backfillCommand.toJSON(), clearvouchesCommand.toJSON()] }
    );
  } else {
    console.warn('Skipping slash registration: CLIENT_ID or GUILD_ID undefined.');
  }
  
  // Initialize vouches object if it doesn't exist
  const existing = await db.get('vouches');
  if (!existing) {
    await db.set('vouches', {});
  }
  
  // Debug: Log current vouches data
  const allVouches = (await db.get('vouches')) || {};
  console.log('Current vouches data:', allVouches);
  const totalVouches = Object.values(allVouches).reduce((sum, count) => sum + count, 0);
  console.log('Total vouches found:', totalVouches);
  
  // rotate playing status with total vouches
  let statusIndex = 0;
  setInterval(async () => {
    // Get total vouches count
    const allVouches = (await db.get('vouches')) || {};
    const totalVouches = Object.values(allVouches).reduce((sum, count) => sum + count, 0);
    
    const statuses = [
      { name: `${totalVouches} total vouches!`, type: ActivityType.Playing },
      { name: 'Image uploads', type: ActivityType.Watching },
      { name: `${totalVouches} vouches counted`, type: ActivityType.Listening },
      { name: 'Tracking progress!', type: ActivityType.Competing },
      { name: 'Building reputation!', type: ActivityType.Playing },
      { name: `Vouch counter: ${totalVouches}`, type: ActivityType.Watching }
    ];
    
    client.user.setPresence({
      activities: [statuses[statusIndex]],
      status: 'online'
    });
    statusIndex = (statusIndex + 1) % statuses.length;
  }, 15000);
  console.log(`Logged in as ${client.user.tag}!`);
});

client.on('messageCreate', async message => {
  if (message.author.bot) return;
  // Only process messages in the specified channel
  if (message.channel.id !== TARGET_CHANNEL_ID) return;

  // Check for image attachments
  const imageAttachments = message.attachments.filter(attachment =>
    attachment.contentType?.startsWith('image/')
  );

  if (imageAttachments.size > 0) {
    const userId = message.author.id;
    const username = message.author.username;

    // Get current vouches from database
    const currentVouches = (await db.get(`vouches.${userId}`)) || 0;
    const newVouches = currentVouches + 1;

    // Update database
    await db.set(`vouches.${userId}`, newVouches);

    // Send notification to the specified channel
    const notificationChannel = client.channels.cache.get(NOTIFICATION_CHANNEL_ID);
    if (notificationChannel) {
      // Check bot permissions in notification channel
      const channelPerms = notificationChannel.permissionsFor(client.user);
      if (channelPerms?.has(PermissionFlagsBits.SendMessages)) {
        try {
          await notificationChannel.send(`@${username} now has ${newVouches} vouches`);
        } catch (err) {
          console.error('[Vouches] Failed to send notification:', err);
        }
      } else {
        console.error(`[Vouches] Missing SendMessages permission in notification channel ${NOTIFICATION_CHANNEL_ID}`);
      }
    } else {
      console.error(`[Vouches] Notification channel ${NOTIFICATION_CHANNEL_ID} not found`);
    }
  }
});

client.on('interactionCreate', async interaction => {
  if (interaction.isChatInputCommand()) {
    // Handle slash commands
    if (interaction.commandName === 'setvouches') {
      if (!interaction.member.permissions.has(PermissionFlagsBits.Administrator)) {
        return interaction.reply({ content: 'You do not have permission to use this command.', flags: MessageFlags.Ephemeral });
      }
      const targetUser = interaction.options.getUser('user');
      const overrideVouches = interaction.options.getInteger('vouches');
      await db.set(`vouches.${targetUser.id}`, overrideVouches);
      await interaction.reply({ content: `Set <@${targetUser.id}>'s vouches to **${overrideVouches}**.`, flags: MessageFlags.Ephemeral });
    }
    
    if (interaction.commandName === 'checkvouches') {
      const targetUser = interaction.options.getUser('user') || interaction.user;
      if (targetUser.id !== interaction.user.id && 
          !interaction.member.permissions.has(PermissionFlagsBits.ManageGuild)) {
        return interaction.reply({ content: 'You do not have permission to check others\' vouches.', flags: MessageFlags.Ephemeral });
      }
      const vouches = (await db.get(`vouches.${targetUser.id}`)) || 0;
      const plural = vouches === 1 ? 'vouch' : 'vouches';
      const mention = targetUser.id === interaction.user.id ? 'You have' : `<@${targetUser.id}> has`;
      await interaction.reply({ content: `${mention} **${vouches}** ${plural}.`, flags: MessageFlags.Ephemeral });
    }

    if (interaction.commandName === 'leaderboard') {
      const limit = interaction.options.getInteger('limit');
      const allVouches = (await db.get('vouches')) || {};
      const allEntries = Object.entries(allVouches)
        .map(([id, vouches]) => ({ id, vouches }))
        .sort((a, b) => b.vouches - a.vouches);
      
      if (allEntries.length === 0) {
        return interaction.reply({ content: 'No vouches have been recorded yet.', flags: MessageFlags.Ephemeral });
      }

      // If limit is specified, use old behavior
      if (limit) {
        const entries = allEntries.slice(0, limit);
        const lines = entries.map((e, i) => {
          const word = e.vouches === 1 ? 'vouch' : 'vouches';
          return `${i + 1}. <@${e.id}> — ${e.vouches} ${word}`;
        });
        
        const content = lines.join('\n');
        
        if (content.length > 2000) {
          // Create text file content with usernames
          const fileLines = [];
          for (let i = 0; i < entries.length; i++) {
            const e = entries[i];
            const word = e.vouches === 1 ? 'vouch' : 'vouches';
            try {
              const user = await client.users.fetch(e.id);
              const username = user.username || `Unknown User (${e.id})`;
              fileLines.push(`${i + 1}. ${username} — ${e.vouches} ${word}`);
            } catch (error) {
              fileLines.push(`${i + 1}. Unknown User (${e.id}) — ${e.vouches} ${word}`);
            }
          }
          
          const fileContent = fileLines.join('\n');
          const attachment = new AttachmentBuilder(Buffer.from(fileContent, 'utf8'), {
            name: 'leaderboard.txt',
            description: 'Vouch Leaderboard'
          });
          
          return interaction.reply({ 
            content: `📊 Leaderboard (${entries.length} users) - sent as file due to length:`,
            files: [attachment],
            flags: MessageFlags.Ephemeral 
          });
        } else {
          return interaction.reply({ content: lines.join('\n'), flags: MessageFlags.Ephemeral });
        }
      }

      // No limit specified - use pagination
      const createLeaderboardPage = (page) => {
        const start = page * 10;
        const end = start + 10;
        const pageEntries = allEntries.slice(start, end);
        
        const lines = pageEntries.map((e, i) => {
          const word = e.vouches === 1 ? 'vouch' : 'vouches';
          const position = start + i + 1;
          return `${position}. <@${e.id}> — ${e.vouches} ${word}`;
        });
        
        const totalPages = Math.ceil(allEntries.length / 10);
        const content = `📊 **Vouch Leaderboard** (Page ${page + 1}/${totalPages})\n\n${lines.join('\n')}`;
        
        const row = new ActionRowBuilder();
        
        // Previous button
        row.addComponents(
          new ButtonBuilder()
            .setCustomId(`leaderboard_prev_${page}`)
            .setLabel('◀ Previous')
            .setStyle(ButtonStyle.Secondary)
            .setDisabled(page === 0)
        );
        
        // Next button
        row.addComponents(
          new ButtonBuilder()
            .setCustomId(`leaderboard_next_${page}`)
            .setLabel('Next ▶')
            .setStyle(ButtonStyle.Secondary)
            .setDisabled(end >= allEntries.length)
        );
        
        return { content, components: [row] };
      };
      
      const pageData = createLeaderboardPage(0);
      await interaction.reply({ 
        content: pageData.content, 
        components: pageData.components, 
        flags: MessageFlags.Ephemeral 
      });
    }

    if (interaction.commandName === 'backfill') {
      if (!interaction.member.permissions.has(PermissionFlagsBits.ManageGuild)) {
        return interaction.reply({ content: 'You do not have permission to use this command.', flags: MessageFlags.Ephemeral });
      }
      await interaction.deferReply({ flags: MessageFlags.Ephemeral });
      const channel = client.channels.cache.get(TARGET_CHANNEL_ID);
      if (!channel || !channel.isTextBased?.()) {
        return interaction.followUp({ content: 'Target channel not found or unsupported.', flags: MessageFlags.Ephemeral });
      }
      // Check bot permissions in target channel
      const perms = channel.permissionsFor(client.user);
      if (!perms?.has(PermissionFlagsBits.ViewChannel) || !perms?.has(PermissionFlagsBits.ReadMessageHistory)) {
        return interaction.followUp({ content: 'I need View Channel and Read Message History permissions to backfill this channel.', flags: MessageFlags.Ephemeral });
      }
      const total = await backfillChannelVouches(channel);
      await interaction.followUp({ content: `Processed **${total}** messages and updated vouches.`, flags: MessageFlags.Ephemeral });
    }

  if (interaction.commandName === 'clearvouches') {
    if (!interaction.member.permissions.has(PermissionFlagsBits.Administrator)) {
      return interaction.reply({ content: 'You do not have permission to clear vouches.', flags: MessageFlags.Ephemeral });
    }
    const targetUser = interaction.options.getUser('user');
    if (targetUser) {
      await db.set(`vouches.${targetUser.id}`, 0);
      return interaction.reply({ content: `Cleared vouches for <@${targetUser.id}>.`, flags: MessageFlags.Ephemeral });
    } else {
      await db.set('vouches', {});
      return interaction.reply({ content: 'Cleared vouches for all users.', flags: MessageFlags.Ephemeral });
    }
  }
  }

  // Handle button interactions for leaderboard pagination
  if (interaction.isButton()) {
    if (interaction.customId.startsWith('leaderboard_')) {
      const [action, direction, currentPageStr] = interaction.customId.split('_');
      const currentPage = parseInt(currentPageStr);
      let newPage = currentPage;
      
      if (direction === 'next') {
        newPage = currentPage + 1;
      } else if (direction === 'prev') {
        newPage = currentPage - 1;
      }
      
      const allVouches = (await db.get('vouches')) || {};
      const allEntries = Object.entries(allVouches)
        .map(([id, vouches]) => ({ id, vouches }))
        .sort((a, b) => b.vouches - a.vouches);
      
      const start = newPage * 10;
      const end = start + 10;
      const pageEntries = allEntries.slice(start, end);
      
      const lines = pageEntries.map((e, i) => {
        const word = e.vouches === 1 ? 'vouch' : 'vouches';
        const position = start + i + 1;
        return `${position}. <@${e.id}> — ${e.vouches} ${word}`;
      });
      
      const totalPages = Math.ceil(allEntries.length / 10);
      const content = `📊 **Vouch Leaderboard** (Page ${newPage + 1}/${totalPages})\n\n${lines.join('\n')}`;
      
      const row = new ActionRowBuilder();
      
      // Previous button
      row.addComponents(
        new ButtonBuilder()
          .setCustomId(`leaderboard_prev_${newPage}`)
          .setLabel('◀ Previous')
          .setStyle(ButtonStyle.Secondary)
          .setDisabled(newPage === 0)
      );
      
      // Next button
      row.addComponents(
        new ButtonBuilder()
          .setCustomId(`leaderboard_next_${newPage}`)
          .setLabel('Next ▶')
          .setStyle(ButtonStyle.Secondary)
          .setDisabled(end >= allEntries.length)
      );
      
      await interaction.update({ 
        content: content, 
        components: [row] 
      });
    }
  }
});

client.login(process.env.DISCORD_TOKEN);