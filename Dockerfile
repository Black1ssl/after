FROM node:18-alpine

WORKDIR /app
COPY . .

RUN npm install telegraf
CMD ["node", "index.js"]
