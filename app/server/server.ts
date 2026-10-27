import { createApp, server } from '@databricks/appkit';
import { registerRoutes } from './routes';
import { settings } from './lib/settings';

// Fail fast on missing or invalid configuration (see app config in resources/review_app.yml).
settings();

createApp({
  plugins: [server()],
  onPluginsReady(appkit) {
    appkit.server.extend((app) => {
      registerRoutes(app);
    });
  },
}).catch((err) => {
  console.error(err);
  process.exit(1);
});
