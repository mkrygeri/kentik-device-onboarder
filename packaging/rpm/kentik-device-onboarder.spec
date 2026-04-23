Name:           kentik-device-onboarder
Version:        1.0.0
Release:        1%{?dist}
Summary:        Automatically onboards unregistered devices into the Kentik platform
License:        Apache-2.0
URL:            https://github.com/kentik/kentik-device-onboarder
BuildArch:      noarch

Requires:       python3
Requires(pre):  shadow-utils
Requires(post): systemd
Requires(preun): systemd
Requires(postun): systemd

%description
kentik-device-onboarder is a lightweight Python daemon that polls a Kentik
flow-pak healthcheck socket, discovers unregistered flow-sending devices, and
automatically registers them with the Kentik API. It runs as a systemd service
with exponential back-off, per-device retry state, and client-side rate limiting.

# ── Sources ──────────────────────────────────────────────────────────────────
# To build this package, place the following files next to the spec:
#   kentik_device_onboarder.py
#   kentik-device-onboarder.service
#   kentik-device-onboarder.env.example
#   packaging/postinst
#   packaging/prerm
#
# Then run from the repository root:
#   rpmbuild -bb --define "_sourcedir $PWD" --define "_specdir $PWD/packaging/rpm" \
#            --define "_builddir $PWD/build" --define "_rpmdir $PWD/dist" \
#            --define "_srcrpmdir $PWD/dist" packaging/rpm/kentik-device-onboarder.spec

%prep
# Nothing to unpack — files are referenced directly from %{_sourcedir}.

%build
# Pure Python — nothing to compile.

%install
install -D -m 0755 %{_sourcedir}/kentik_device_onboarder.py \
    %{buildroot}/opt/kentik-device-onboarder/kentik_device_onboarder.py

install -D -m 0644 %{_sourcedir}/kentik-device-onboarder.service \
    %{buildroot}/usr/lib/systemd/system/kentik-device-onboarder.service

install -D -m 0644 %{_sourcedir}/kentik-device-onboarder.env.example \
    %{buildroot}/etc/kentik-device-onboarder/onboarder.env.example

%pre
# Create system group and user before files are laid down.
getent group kentik-onboarder >/dev/null 2>&1 || \
    groupadd --system kentik-onboarder

id -u kentik-onboarder >/dev/null 2>&1 || \
    useradd \
        --system \
        --gid kentik-onboarder \
        --home-dir /opt/kentik-device-onboarder \
        --no-create-home \
        --shell /usr/sbin/nologin \
        kentik-onboarder

exit 0

%post
STATE_DIR=/var/lib/kentik-device-onboarder
CONFIG_DIR=/etc/kentik-device-onboarder
INSTALL_DIR=/opt/kentik-device-onboarder

discover_kproxy_credentials() {
    pid=$(pgrep -n kproxy 2>/dev/null || true)
    if [ -z "$pid" ] || [ ! -r "/proc/$pid/environ" ]; then
        return 1
    fi

    KPROXY_KENTIK_API_EMAIL=$(tr '\0' '\n' < "/proc/$pid/environ" | sed -n 's/^KENTIK_API_EMAIL=//p' | tail -n 1)
    KPROXY_KENTIK_API_TOKEN=$(tr '\0' '\n' < "/proc/$pid/environ" | sed -n 's/^KENTIK_API_TOKEN=//p' | tail -n 1)

    [ -n "$KPROXY_KENTIK_API_EMAIL" ] && [ -n "$KPROXY_KENTIK_API_TOKEN" ]
}

escape_sed_replacement() {
    printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

populate_credentials_in_config() {
    config_file="$1"

    if ! discover_kproxy_credentials; then
        echo "kproxy credentials not found in process environment; leaving API credentials unchanged"
        return 0
    fi

    email_escaped=$(escape_sed_replacement "$KPROXY_KENTIK_API_EMAIL")
    token_escaped=$(escape_sed_replacement "$KPROXY_KENTIK_API_TOKEN")

    sed -i \
        -e "s|^KENTIK_API_EMAIL=.*$|KENTIK_API_EMAIL=${email_escaped}|" \
        -e "s|^KENTIK_API_TOKEN=.*$|KENTIK_API_TOKEN=${token_escaped}|" \
        "$config_file"

    echo "populated KENTIK_API_EMAIL and KENTIK_API_TOKEN from kproxy environment"
}

install -d -m 0750 -o kentik-onboarder -g kentik-onboarder "$STATE_DIR"

chown root:kentik-onboarder "$CONFIG_DIR"
chmod 0750 "$CONFIG_DIR"

if [ ! -f "$CONFIG_DIR/onboarder.env" ]; then
    install -m 0640 -o kentik-onboarder -g kentik-onboarder \
        "$CONFIG_DIR/onboarder.env.example" \
        "$CONFIG_DIR/onboarder.env"
    populate_credentials_in_config "$CONFIG_DIR/onboarder.env"
    echo "Created initial configuration: $CONFIG_DIR/onboarder.env"
    echo "Edit this file before starting the service."
fi

chown root:kentik-onboarder "$INSTALL_DIR"
chown kentik-onboarder:kentik-onboarder "$INSTALL_DIR/kentik_device_onboarder.py"

%systemd_post kentik-device-onboarder.service

%preun
%systemd_preun kentik-device-onboarder.service

%postun
%systemd_postun_with_restart kentik-device-onboarder.service

%files
%attr(0755, root, kentik-onboarder) /opt/kentik-device-onboarder
%attr(0755, kentik-onboarder, kentik-onboarder) /opt/kentik-device-onboarder/kentik_device_onboarder.py
%attr(0755, root, kentik-onboarder) /etc/kentik-device-onboarder
%config(noreplace) %attr(0644, root, root) /etc/kentik-device-onboarder/onboarder.env.example
%attr(0644, root, root) /usr/lib/systemd/system/kentik-device-onboarder.service

%changelog
* Tue Apr 22 2026 Kentik Technologies, Inc. <support@kentik.com> - 1.0.0-1
- Initial package release
