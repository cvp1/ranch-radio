// codecvol: read or set the output volume (0..1 scalar) of a CoreAudio device by name.
// usage: codecvol "USB Audio CODEC" [new_volume]
#include <CoreAudio/CoreAudio.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static AudioObjectID find(const char *want) {
	AudioObjectPropertyAddress a = {kAudioHardwarePropertyDevices, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};
	UInt32 sz = 0;
	AudioObjectGetPropertyDataSize(kAudioObjectSystemObject, &a, 0, NULL, &sz);
	AudioObjectID *ids = malloc(sz);
	AudioObjectGetPropertyData(kAudioObjectSystemObject, &a, 0, NULL, &sz, ids);
	for (UInt32 i = 0; i < sz / sizeof(AudioObjectID); i++) {
		CFStringRef name; UInt32 nsz = sizeof(name); char buf[256];
		AudioObjectPropertyAddress na = {kAudioObjectPropertyName, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};
		if (AudioObjectGetPropertyData(ids[i], &na, 0, NULL, &nsz, &name)) continue;
		CFStringGetCString(name, buf, sizeof(buf), kCFStringEncodingUTF8); CFRelease(name);
		AudioObjectPropertyAddress oa = {kAudioDevicePropertyStreams, kAudioObjectPropertyScopeOutput, kAudioObjectPropertyElementMain};
		UInt32 osz = 0; AudioObjectGetPropertyDataSize(ids[i], &oa, 0, NULL, &osz);
		if (osz && strstr(buf, want)) { AudioObjectID id = ids[i]; free(ids); return id; }
	}
	free(ids); return 0;
}

int main(int argc, char **argv) {
	if (argc < 2) { fprintf(stderr, "usage: %s name [volume]\n", argv[0]); return 2; }
	AudioObjectID dev = find(argv[1]);
	if (!dev) { fprintf(stderr, "no output device matching %s\n", argv[1]); return 1; }
	int found = 0;
	for (UInt32 el = 0; el <= 2; el++) {
		AudioObjectPropertyAddress va = {kAudioDevicePropertyVolumeScalar, kAudioObjectPropertyScopeOutput, el};
		if (!AudioObjectHasProperty(dev, &va)) continue;
		found = 1;
		Float32 v; UInt32 vsz = sizeof(v);
		AudioObjectGetPropertyData(dev, &va, 0, NULL, &vsz, &v);
		printf("element %u: volume %.3f", el, v);
		if (argc > 2) {
			Boolean settable = 0; AudioObjectIsPropertySettable(dev, &va, &settable);
			Float32 nv = atof(argv[2]);
			if (settable && !AudioObjectSetPropertyData(dev, &va, 0, NULL, sizeof(nv), &nv)) {
				AudioObjectGetPropertyData(dev, &va, 0, NULL, &vsz, &v);
				printf(" -> %.3f", v);
			} else printf(" (not settable)");
		}
		AudioObjectPropertyAddress da = {kAudioDevicePropertyVolumeDecibels, kAudioObjectPropertyScopeOutput, el};
		Float32 db; UInt32 dsz = sizeof(db);
		if (!AudioObjectGetPropertyData(dev, &da, 0, NULL, &dsz, &db)) printf("  (%.1f dB)", db);
		printf("\n");
	}
	if (!found) { printf("device has no software output volume control\n"); return 3; }
	return 0;
}
