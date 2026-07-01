# Accounts

The [Collection](collection.md) is fully browsable **without an account**. A free account lets you **submit Specimens** and **react** to others'. Sign up with **email + password** or **GitHub**.

## Email verification

When you register with email + password, embody.tools sends a **verification link** to your address. Click it to confirm your email, then sign in. Until your email is verified, sign-in is held — so if you aren't signed in right after registering, check your inbox (and use the **Resend** option on the sign-in page if the email didn't arrive).

## Password reset

Forgot your password? Request a reset link from the sign-in page; it's emailed to you and expires in one hour.

## Your profile

Each account has a public profile at `/u/<handle>` listing the Specimens you've published. Your account menu (top-right, once signed in) links to your profile and signs you out.

!!! note "Self-hosting"
    Email (verification, reset, owner notifications) is optional and configured via Resend; admin access to the moderation panel is controlled by an `ADMIN_EMAILS` allowlist or a user's `trust_level`. See the platform `README` for the full environment/config reference.
