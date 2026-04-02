const LOCAL_ANDROID = "http://10.0.2.2:5000";
const LOCAL_DEVICE = "http://192.168.1.10:5000";
const PRODUCTION_DEFAULT = "https://www.edu-path.app";

function cleanUrl(url) {
	if (!url || typeof url !== "string") {
		return "";
	}
	let value = url.trim();
	value = value.replace(/\\+/g, "/");
	if (!/^https?:\/\//i.test(value)) {
		value = `https://${value.replace(/^\/+/, "")}`;
	}
	return value.replace(/\/$/, "");
}

// Set EXPO_PUBLIC_BASE_URL in mobile_app/.env for local dev,
// or in EAS secrets for cloud builds.
const ENV_BASE_URL = cleanUrl(process.env.EXPO_PUBLIC_BASE_URL);
const PROD_URL = cleanUrl(PRODUCTION_DEFAULT);

export const BASE_URL = ENV_BASE_URL || PROD_URL;

export const FALLBACK_URLS = [
	cleanUrl(process.env.EXPO_PUBLIC_FALLBACK_1) || LOCAL_ANDROID,
	cleanUrl(process.env.EXPO_PUBLIC_FALLBACK_2) || LOCAL_DEVICE,
	PROD_URL,
].filter(Boolean);
