# Sourced by bootstrap.sh. Inputs are HOST files; only staged copies enter Docker.
tls_error() { echo "error: TLS $*" >&2; return 1; }

tls_source() {
    local value="$1"
    case "$value" in
        /run/h-mesh/tls/*) printf '%s/%s' "$TLS_DIR" "${value##*/}" ;;
        *) printf '%s' "$value" ;;
    esac
}

tls_copy() {
    local source="$1" destination="$2"
    [ -f "$source" ] && [ -r "$source" ] || {
        tls_error "material must be a readable regular HOST file ($destination)"; return 1;
    }
    cp -- "$source" "$TLS_STAGE/$destination" || { tls_error "cannot copy host material"; return 1; }
}

tls_pair() {
    local cert="$1" key="$2" name="$3" cert_public key_public
    if [ -z "$cert" ] && [ -z "$key" ]; then return 0; fi
    [ -n "$cert" ] && [ -n "$key" ] || { tls_error "certificate requires its key ($name)"; return 1; }
    tls_copy "$(tls_source "$cert")" "$name.crt" && tls_copy "$(tls_source "$key")" "$name.key" || return 1
    grep -q -- '-----BEGIN CERTIFICATE-----' "$TLS_STAGE/$name.crt" &&
        openssl x509 -in "$TLS_STAGE/$name.crt" -noout >/dev/null 2>&1 &&
        openssl crl2pkcs7 -nocrl -certfile "$TLS_STAGE/$name.crt" -out /dev/null >/dev/null 2>&1 || {
            tls_error "certificate is not usable PEM ($name)"; return 1;
        }
    grep -Eq -- '-----BEGIN ([A-Z0-9]+ )*PRIVATE KEY-----' "$TLS_STAGE/$name.key" &&
        openssl pkey -in "$TLS_STAGE/$name.key" -passin pass: -check -noout >/dev/null 2>&1 || {
            tls_error "key must be usable unencrypted PEM ($name)"; return 1;
        }
    cert_public="$(openssl x509 -in "$TLS_STAGE/$name.crt" -pubkey -noout 2>/dev/null)" || return 1
    key_public="$(openssl pkey -in "$TLS_STAGE/$name.key" -passin pass: -pubout 2>/dev/null)" || return 1
    [ "$cert_public" = "$key_public" ] || { tls_error "certificate and key do not match ($name)"; return 1; }
}

stage_tls() {
    local host_uid staged target ca_source
    TLS_DIR="$(cd "$(dirname "$ENV_FILE")" && pwd)/tls"
    TLS_MOUNT=0
    if [ -z "$TLS_CERT$TLS_KEY$SESSION_CERT$SESSION_KEY$CA_CERT" ]; then return 0; fi
    command -v openssl >/dev/null 2>&1 || { tls_error "validation requires openssl on the host"; return 1; }
    host_uid="$(id -u)"
    case "$host_uid" in
        0|1000) ;;
        *) tls_error "permission: host UID must be 1000 or root to stage private files for container UID 1000; Compose was not started"; return 1 ;;
    esac
    [ ! -L "$TLS_DIR" ] && { [ ! -e "$TLS_DIR" ] || [ -d "$TLS_DIR" ]; } || {
        tls_error "staging directory must be a real directory"; return 1;
    }
    TLS_STAGE="$(umask 077; mktemp -d "$(dirname "$ENV_FILE")/.tls.XXXXXX")" || return 1
    # The subshell trap only removes this invocation's private staging directory.
    (
        trap 'rm -rf -- "$TLS_STAGE"' EXIT
        tls_pair "$TLS_CERT" "$TLS_KEY" api && tls_pair "$SESSION_CERT" "$SESSION_KEY" session || exit 1
        if [ -n "$CA_CERT" ]; then
            ca_source="$(tls_source "$CA_CERT")"
            tls_copy "$ca_source" ca.crt || exit 1
            [ "$(wc -c < "$TLS_STAGE/ca.crt")" -le 4194304 ] &&
                grep -q -- '-----BEGIN CERTIFICATE-----' "$TLS_STAGE/ca.crt" &&
                openssl x509 -in "$TLS_STAGE/ca.crt" -noout >/dev/null 2>&1 &&
                openssl crl2pkcs7 -nocrl -certfile "$TLS_STAGE/ca.crt" -out /dev/null >/dev/null 2>&1 || {
                    tls_error "CA must be a usable PEM bundle no larger than 4 MiB"; exit 1;
                }
        fi
        (umask 077; mkdir -p -- "$TLS_DIR") || exit 1
        chmod 0700 "$TLS_DIR" || exit 1
        for staged in "$TLS_STAGE"/*; do
            target="$TLS_DIR/${staged##*/}"
            [ ! -L "$target" ] || { tls_error "staged material must not be a symlink"; exit 1; }
            [ ! -e "$target" ] || [ -f "$target" ] || { tls_error "staged material must be a regular file"; exit 1; }
            chmod 0600 "$staged" || exit 1
            # Root only changes the copies, never the operator's source files.
            [ "$host_uid" != 0 ] || chown 1000 "$staged" || exit 1
            if ! cmp -s -- "$staged" "$target"; then mv -f -- "$staged" "$target" || exit 1; fi
            chmod 0600 "$target" || exit 1
            [ "$host_uid" != 0 ] || chown 1000 "$target" || exit 1
        done
        [ "$host_uid" != 0 ] || chown 1000 "$TLS_DIR" || exit 1
    ) || return 1
    [ -z "$TLS_CERT" ] || { TLS_CERT=/run/h-mesh/tls/api.crt; TLS_KEY=/run/h-mesh/tls/api.key; }
    [ -z "$SESSION_CERT" ] || { SESSION_CERT=/run/h-mesh/tls/session.crt; SESSION_KEY=/run/h-mesh/tls/session.key; }
    [ -z "$CA_CERT" ] || CA_CERT=/run/h-mesh/tls/ca.crt
    upsert_env_line H_MESH_TLS_DIR "$TLS_DIR" || return 1
    TLS_MOUNT=1
}
