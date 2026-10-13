# Changelog

## [7.0.0](https://github.com/wthueb/tarr/compare/v6.0.0...v7.0.0) (2026-10-13)


### ⚠ BREAKING CHANGES

* set seed limits per-tracker instead of per category

### Features

* configuration yaml schema ([192eb8f](https://github.com/wthueb/tarr/commit/192eb8fcbd3bb6e029ec2f27f68f8774a0ed210e))


### Code Refactoring

* set seed limits per-tracker instead of per category ([d0bf3b0](https://github.com/wthueb/tarr/commit/d0bf3b071e2b2ba487d61400882aedfa70074f40))

## [6.0.0](https://github.com/wthueb/tarr/compare/v5.2.0...v6.0.0) (2026-10-12)


### ⚠ BREAKING CHANGES

* move polling and job settings to the root
* require explicit actions for seed limit categories

### Features

* require explicit actions for seed limit categories ([dfcfb0e](https://github.com/wthueb/tarr/commit/dfcfb0e5f6bc1370a180ad2bf7fb5cf4a764a182))
* support forced tracker limits per category ([9009c0a](https://github.com/wthueb/tarr/commit/9009c0a41c82f9a50fbacb92a154bb62e1e09d0b))
* verify arr queues before removing torrents ([9723f69](https://github.com/wthueb/tarr/commit/9723f69abc271ba09a81c969ffa901c03fe844ea))


### Code Refactoring

* move polling and job settings to the root ([053282b](https://github.com/wthueb/tarr/commit/053282b3c8c063084b0fb722271086799b6e7eda))

## [5.2.0](https://github.com/wthueb/tarr/compare/v5.1.1...v5.2.0) (2026-10-11)


### Features

* maintain_free_space tracker delete priority ([0507367](https://github.com/wthueb/tarr/commit/05073674b6976bf65068e1673f13b84a4354eddd))


### Bug Fixes

* maintain_free_space accounts for hard links ([4e1c0e2](https://github.com/wthueb/tarr/commit/4e1c0e21f5d6f18fa8a0fdfc177d0aafd1be50fe))
* maintain_free_space remove in reverse seeder count order ([750df77](https://github.com/wthueb/tarr/commit/750df77c2654abacf4318403f036e155342cc67c))

## [5.1.1](https://github.com/wthueb/tarr/compare/v5.1.0...v5.1.1) (2026-10-10)


### Bug Fixes

* respect seed limits in remove_stopped ([e6397e1](https://github.com/wthueb/tarr/commit/e6397e1d800c03ec7586a8a2c658e4548a5d9c2d))

## [5.1.0](https://github.com/wthueb/tarr/compare/v5.0.0...v5.1.0) (2026-10-05)


### Features

* cleanup empty directories ([8cfd0ca](https://github.com/wthueb/tarr/commit/8cfd0cac1a9a9498da1d187b78f3f591262c50d7))

## [5.0.0](https://github.com/wthueb/tarr/compare/v4.0.0...v5.0.0) (2026-08-20)


### ⚠ BREAKING CHANGES

* restructure config to nest under qbittorrent
* rename to tarr

### Features

* rename to tarr ([372c116](https://github.com/wthueb/tarr/commit/372c1167d054a16e1cc919ad44b7ec518d85ff0f))
* restructure config to nest under qbittorrent ([4adc6c4](https://github.com/wthueb/tarr/commit/4adc6c424962fd49f5a8337102a77058afe613f6))


### Bug Fixes

* change free space maintenance sort order to ratio/seed time ([3e9c616](https://github.com/wthueb/tarr/commit/3e9c61679e9ef60a6464906de62342e7b54d0432))

## [4.0.0](https://github.com/wthueb/tarr/compare/v3.2.1...v4.0.0) (2026-08-13)


### ⚠ BREAKING CHANGES

* per-category overrides for seed limits

### Features

* per-category overrides for seed limits ([827ee62](https://github.com/wthueb/tarr/commit/827ee6273c4b5ee68a8ccb88dcb6a749257222fc))

## [3.2.1](https://github.com/wthueb/tarr/compare/v3.2.0...v3.2.1) (2026-07-31)


### Bug Fixes

* remove_completed -&gt; remove_stopped ([4257cc5](https://github.com/wthueb/tarr/commit/4257cc553b0c367d13c9685dbdc6a62f17b51fb3))

## [3.2.0](https://github.com/wthueb/tarr/compare/v3.1.1...v3.2.0) (2026-07-30)


### Features

* remove completed torrents ([e46f6fb](https://github.com/wthueb/tarr/commit/e46f6fb3e62744f23fa4a2f65a14ee83b92ff2f0))
* support categories and ignore_categories with every feature ([ad3f1f4](https://github.com/wthueb/tarr/commit/ad3f1f4ffa80ede7fce7136e1cb1bd386ad85a88))


### Bug Fixes

* "torrent has been deleted" as unregistered message ([9a56850](https://github.com/wthueb/tarr/commit/9a56850de2536587219be8342fbfb046152bbc27))

## [3.1.1](https://github.com/wthueb/tarr/compare/v3.1.0...v3.1.1) (2026-07-27)


### Bug Fixes

* use case-insensitive unregistered check ([ae10f84](https://github.com/wthueb/tarr/commit/ae10f84fd0f2a8c226e13560fdff711558579f41))

## [3.1.0](https://github.com/wthueb/tarr/compare/v3.0.0...v3.1.0) (2026-07-11)


### Features

* add remove_unregistered.ignore_categories to config ([1fc9207](https://github.com/wthueb/tarr/commit/1fc92076e17c1a55b63a7244f4b655f0d6e5f59d))


### Bug Fixes

* remove unregistered torrents with different message format ([44f6712](https://github.com/wthueb/tarr/commit/44f67128326f402ff0df9e7fdf13b4b9fadb9736))

## [3.0.0](https://github.com/wthueb/tarr/compare/v2.0.0...v3.0.0) (2026-07-07)


### ⚠ BREAKING CHANGES

* set seed limits on certain categories based on trackers

### Features

* set seed limits on certain categories based on trackers ([00f452b](https://github.com/wthueb/tarr/commit/00f452b09e7d0b926c11cc01964fc1964d85e863))


### Bug Fixes

* pass inactive_seeding_time_limit when setting seed limits ([d7228f5](https://github.com/wthueb/tarr/commit/d7228f590e22b8d5b70ddbb70763ae252f763dd9))

## [2.0.0](https://github.com/wthueb/tarr/compare/v1.1.0...v2.0.0) (2026-07-06)


### ⚠ BREAKING CHANGES

* move config to yaml, set seed limits per tracker

### Features

* move config to yaml, set seed limits per tracker ([928b6a1](https://github.com/wthueb/tarr/commit/928b6a101ff563c1d99a9630d8d97a9662cb01f0))
