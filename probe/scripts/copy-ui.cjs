const fs = require('node:fs');
fs.cpSync('ui', 'dist/ui', { recursive: true });
