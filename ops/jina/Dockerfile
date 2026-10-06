FROM ghcr.io/jina-ai/reader@sha256:8cc9a5cf6dc9c240235fccc0026661de74b6e13c21b36e6a86b3281b9ef10f28
USER root
RUN node -e "const fs=require('fs');const p='/app/build/api/crawler.js';let s=fs.readFileSync(p,'utf8');const x=\"await auth.assertTier(0, 'Browser rendered HTML');\";if(!s.includes(x))throw Error('OSS patch target missing');fs.writeFileSync(p,s.replace(x,'/* Own OSS instance: browser HTML requires no SaaS subscription. */'));"

USER jina
