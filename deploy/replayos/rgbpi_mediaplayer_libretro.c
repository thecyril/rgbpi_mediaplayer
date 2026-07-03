/* Minimal libretro stub whose only job is to fire the media player launcher
 * when RePlay loads it, then idle until replay.service is stopped by that
 * launcher (replay_launch.sh stops RePlay, runs the player, restarts RePlay
 * on exit). systemd-run detaches the launcher into its own cgroup so it
 * survives `systemctl stop replay.service`.
 *
 * Build on the Pi:
 *   gcc -O2 -shared -fPIC -o rgbpi_mediaplayer_libretro.so rgbpi_mediaplayer_libretro.c
 * Install (see README.md): point a cores.cfg section at the .so.
 */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

struct retro_game_geometry {
    unsigned base_width, base_height, max_width, max_height;
    float aspect_ratio;
};
struct retro_system_timing {
    double fps, sample_rate;
};
struct retro_system_av_info {
    struct retro_game_geometry geometry;
    struct retro_system_timing timing;
};
struct retro_system_info {
    const char *library_name, *library_version, *valid_extensions;
    bool need_fullpath, block_extract;
};
struct retro_game_info {
    const char *path;
    const void *data;
    size_t size;
    const char *meta;
};

typedef bool (*retro_environment_t)(unsigned cmd, void *data);
typedef void (*retro_video_refresh_t)(const void *data, unsigned width, unsigned height, size_t pitch);
typedef void (*retro_audio_sample_t)(int16_t left, int16_t right);
typedef size_t (*retro_audio_sample_batch_t)(const int16_t *data, size_t frames);
typedef void (*retro_input_poll_t)(void);
typedef int16_t (*retro_input_state_t)(unsigned port, unsigned device, unsigned index, unsigned id);

static retro_video_refresh_t video_cb;
static retro_input_poll_t input_poll_cb;
static uint16_t framebuffer[320 * 240];

void retro_set_environment(retro_environment_t cb) { (void)cb; }
void retro_set_video_refresh(retro_video_refresh_t cb) { video_cb = cb; }
void retro_set_audio_sample(retro_audio_sample_t cb) { (void)cb; }
void retro_set_audio_sample_batch(retro_audio_sample_batch_t cb) { (void)cb; }
void retro_set_input_poll(retro_input_poll_t cb) { input_poll_cb = cb; }
void retro_set_input_state(retro_input_state_t cb) { (void)cb; }

unsigned retro_api_version(void) { return 1; }
void retro_init(void) {}
void retro_deinit(void) {}

void retro_get_system_info(struct retro_system_info *info)
{
    memset(info, 0, sizeof(*info));
    info->library_name = "rgbpi_mediaplayer";
    info->library_version = "1.0";
    /* "lr" covers the Extra-menu slot; the media extensions cover the
     * Alpha Player main-menu slot (the launcher file is a 0-byte .mkv —
     * need_fullpath keeps RePlay from trying to load its content). */
    info->valid_extensions = "lr|mkv|avi|mp4|mp3|flac|ogg|m4a|webm|mov|wmv|mpg|mpeg|vob|ts|m3u";
    info->need_fullpath = true;
}

void retro_get_system_av_info(struct retro_system_av_info *info)
{
    memset(info, 0, sizeof(*info));
    info->geometry.base_width = 320;
    info->geometry.base_height = 240;
    info->geometry.max_width = 320;
    info->geometry.max_height = 240;
    info->geometry.aspect_ratio = 4.0f / 3.0f;
    info->timing.fps = 60.0;
    info->timing.sample_rate = 44100.0;
}

void retro_set_controller_port_device(unsigned port, unsigned device)
{
    (void)port;
    (void)device;
}

void retro_reset(void) {}

void retro_run(void)
{
    if (input_poll_cb)
        input_poll_cb();
    if (video_cb)
        video_cb(framebuffer, 320, 240, 320 * sizeof(uint16_t));
}

bool retro_load_game(const struct retro_game_info *game)
{
    (void)game;
    system("systemd-run --collect --quiet /opt/rgbpi_mediaplayer/replay_launch.sh");
    return true;
}

void retro_unload_game(void) {}
unsigned retro_get_region(void) { return 0; }

bool retro_load_game_special(unsigned type, const struct retro_game_info *info, size_t num)
{
    (void)type;
    (void)info;
    (void)num;
    return false;
}

size_t retro_serialize_size(void) { return 0; }
bool retro_serialize(void *data, size_t size) { (void)data; (void)size; return false; }
bool retro_unserialize(const void *data, size_t size) { (void)data; (void)size; return false; }
void retro_cheat_reset(void) {}
void retro_cheat_set(unsigned index, bool enabled, const char *code)
{
    (void)index;
    (void)enabled;
    (void)code;
}
void *retro_get_memory_data(unsigned id) { (void)id; return NULL; }
size_t retro_get_memory_size(unsigned id) { (void)id; return 0; }
