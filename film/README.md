# Overview film

The 29-second loop on the overview page, written as HTML scenes and rendered with [HyperFrames](https://github.com/heygen-com/hyperframes).
Each scene rebuilds one Gatehouse screen with its real labels: Today, the payment attempt, the account record and the tenant message.

```
npx hyperframes@0.9.19 check
npx hyperframes@0.9.19 render --output renders/gatehouse-film.mp4
```

The page serves the result from `desk/static/film/` as MP4, a smaller WebM and a poster frame.
