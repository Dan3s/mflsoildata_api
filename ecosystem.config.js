// pm2 process definition for the API (used by the Jenkinsfile; `pm2 startOrRestart ecosystem.config.js`).
//
// MFL_UVICORN is the uvicorn of the conda env (the Jenkinsfile exports it); MFL_API_PORT is the
// port to listen on. Both have defaults so the file also works when started by hand.
// The API listens on this machine only: the frontend reaches it server-side (API_URL) and the
// browser goes through the frontend's /api/backend proxy. Set MFL_API_HOST=0.0.0.0 to expose it.
module.exports = {
  apps: [
    {
      name: "mfl_api",
      cwd: `${__dirname}/src`,
      script: process.env.MFL_UVICORN || "uvicorn",
      args: `main:app --host ${process.env.MFL_API_HOST || "127.0.0.1"} --port ${process.env.MFL_API_PORT || 8090} --workers 2`,
      interpreter: "none",
      autorestart: true,
      max_restarts: 10,
    },
  ],
};
