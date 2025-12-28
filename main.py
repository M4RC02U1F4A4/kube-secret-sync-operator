import kopf
import kubernetes.client
from kubernetes.client.rest import ApiException
import logging
import time

# Annotation keys
SYNC_SOURCE_ANNOTATION = "kss-operator/source-namespace"
SYNC_TIMESTAMP_ANNOTATION = "kss-operator/synced-at"

def is_synced_secret(secret) -> bool:
    """Verifica se un secret è stato sincronizzato (non è l'originale)"""
    annotations = secret.metadata.annotations or {}
    return SYNC_SOURCE_ANNOTATION in annotations

def sync_secret(secret_name, namespace, secret_data):
    api = kubernetes.client.CoreV1Api()
    
    try:
        namespaces = api.list_namespace().items
    except ApiException as e:
        logging.error(f"Failed to list namespaces: {e}")
        return
    
    success_count = 0
    fail_count = 0
    
    for ns in namespaces:
        ns_name = ns.metadata.name
        if ns_name == namespace: 
            continue
        
        logging.info(f"Synching {secret_name} secret in {ns_name} namespace...")
        
        try:
            existing_secret = api.read_namespaced_secret(secret_name, ns_name)
            
            # Check if existing secret is a synced copy
            if is_synced_secret(existing_secret):
                api.delete_namespaced_secret(secret_name, ns_name)
            else:
                logging.warning(
                    f"Secret {secret_name} in {ns_name} exists but is not synced. "
                    f"Skipping to avoid overwriting manual secret."
                )
                continue
                
        except ApiException as e:
            if e.status != 404:
                logging.error(f"Error reading/deleting secret {secret_name} in {ns_name}: {e}")
                fail_count += 1
                continue
        
        # Crea il nuovo secret con annotazioni
        try:
            new_secret = kubernetes.client.V1Secret(
                metadata=kubernetes.client.V1ObjectMeta(
                    name=secret_name,
                    labels={
                        "kss-operator/sync": "synced"
                    },
                    annotations={
                        SYNC_SOURCE_ANNOTATION: namespace,
                        SYNC_TIMESTAMP_ANNOTATION: str(int(time.time()))
                    }
                ),
                data=secret_data.data,
                type=secret_data.type
            )
            api.create_namespaced_secret(namespace=ns_name, body=new_secret)
            success_count += 1
        except ApiException as e:
            logging.error(f"Failed to create secret {secret_name} in {ns_name}: {e}")
            fail_count += 1
    
    logging.info(f"Sync completed for {secret_name}: {success_count} succeeded, {fail_count} failed")

def delete_synced_secrets(secret_name):
    api = kubernetes.client.CoreV1Api()
    
    try:
        namespaces = api.list_namespace().items
    except ApiException as e:
        logging.error(f"Failed to list namespaces: {e}")
        return
    
    for ns in namespaces:
        ns_name = ns.metadata.name
        try:
            secret = api.read_namespaced_secret(secret_name, ns_name)
            
            # Delete only if it's a synced secret
            if is_synced_secret(secret):
                logging.info(f"Deleting synced secret {secret_name} from {ns_name} namespace...")
                api.delete_namespaced_secret(secret_name, ns_name)
            else:
                logging.info(f"Secret {secret_name} in {ns_name} is not synced, skipping deletion")
                
        except ApiException as e:
            if e.status == 404:
                continue
            else:
                logging.error(f"Error deleting secret {secret_name} from {ns_name}: {e}")

@kopf.on.create('v1', 'Secret', labels={"kss-operator/sync": "sync"})
@kopf.on.update('v1', 'Secret', labels={"kss-operator/sync": "sync"})
def sync_secret_handler(spec, meta, namespace, body, **kwargs):
    secret_name = meta['name']
    
    # Filter out synced copies
    if is_synced_secret(body):
        logging.debug(f"Skipping synced copy of {secret_name} in {namespace}")
        return
    
    api = kubernetes.client.CoreV1Api()
    
    try:
        secret = api.read_namespaced_secret(secret_name, namespace)
        logging.info(f"Secret {secret_name} created/updated in {namespace}, syncing with all other namespaces.")
        sync_secret(secret_name, namespace, secret)
    except ApiException as e:
        logging.error(f"Failed to read secret {secret_name} in {namespace}: {e}")
        raise kopf.TemporaryError(f"Failed to read source secret: {e}", delay=30)

@kopf.on.delete('v1', 'Secret', labels={"kss-operator/sync": "sync"})
def delete_synced_secret_handler(meta, namespace, body, **kwargs):
    secret_name = meta['name']
    
    # Filter out synced copies
    if is_synced_secret(body):
        logging.debug(f"Skipping deletion of synced copy {secret_name} in {namespace}")
        return
    
    logging.info(f"Secret {secret_name} deleted from {namespace} namespace, removing from all other namespaces.")
    delete_synced_secrets(secret_name)

@kopf.on.create('v1', 'Namespace')
def sync_secrets_in_new_namespace_handler(spec, meta, **kwargs):
    new_namespace = meta['name']
    logging.info(f"New namespace {new_namespace} created, syncing secrets...")
    
    api = kubernetes.client.CoreV1Api()
    
    try:
        secrets = api.list_secret_for_all_namespaces(label_selector="kss-operator/sync=sync").items
    except ApiException as e:
        logging.error(f"Failed to list secrets: {e}")
    
    # Filter out synced secrets
    source_secrets = [s for s in secrets if not is_synced_secret(s)]
    
    for secret in source_secrets:
        secret_name = secret.metadata.name
        logging.info(f"Syncing secret {secret_name} to {new_namespace} namespace...")
        try:
            sync_secret(secret_name, secret.metadata.namespace, secret)
        except kopf.TemporaryError:
            logging.warning(f"Failed to sync {secret_name}, will retry later")
            continue

@kopf.timer('v1', 'Secret', labels={"kss-operator/sync": "sync"}, interval=300.0)
def reconcile_secret(meta, namespace, body, **kwargs):
    """
    Periodic reconciliation every 5 minutes.
    Ensures all secrets are properly synchronized and recovers any events missed during controller downtime.
    """
    secret_name = meta['name']
    
    # Filter out synced copies
    if is_synced_secret(body):
        return
    
    logging.info(f"Reconciling secret {secret_name} in namespace {namespace}")
    
    api = kubernetes.client.CoreV1Api()
    
    try:
        source_secret = api.read_namespaced_secret(secret_name, namespace)
        namespaces = api.list_namespace().items
        
        for ns in namespaces:
            ns_name = ns.metadata.name
            if ns_name == namespace:
                continue
            
            try:
                # Check if the secret exists in the target namespace
                existing = api.read_namespaced_secret(secret_name, ns_name)
                
                # Only update if it's a synced secret
                if is_synced_secret(existing):
                    # Confronta i dati
                    if existing.data != source_secret.data or existing.type != source_secret.type:
                        logging.info(f"Secret {secret_name} in {ns_name} is out of sync, updating...")
                        # Delete and recreate
                        api.delete_namespaced_secret(secret_name, ns_name)
                        import time
                        new_secret = kubernetes.client.V1Secret(
                            metadata=kubernetes.client.V1ObjectMeta(
                                name=secret_name,
                                labels={"kss-operator/sync": "synced"},
                                annotations={
                                    SYNC_SOURCE_ANNOTATION: namespace,
                                    SYNC_TIMESTAMP_ANNOTATION: str(int(time.time()))
                                }
                            ),
                            data=source_secret.data,
                            type=source_secret.type
                        )
                        api.create_namespaced_secret(namespace=ns_name, body=new_secret)
                        
            except ApiException as e:
                if e.status == 404:
                    # Secret does not exist, create it
                    logging.info(f"Secret {secret_name} missing in {ns_name}, creating...")
                    import time
                    new_secret = kubernetes.client.V1Secret(
                        metadata=kubernetes.client.V1ObjectMeta(
                            name=secret_name,
                            labels={"kss-operator/sync": "synced"},
                            annotations={
                                SYNC_SOURCE_ANNOTATION: namespace,
                                SYNC_TIMESTAMP_ANNOTATION: str(int(time.time()))
                            }
                        ),
                        data=source_secret.data,
                        type=source_secret.type
                    )
                    api.create_namespaced_secret(namespace=ns_name, body=new_secret)
                else:
                    logging.error(f"Error checking secret {secret_name} in {ns_name}: {e}")
                    
    except ApiException as e:
        logging.error(f"Error during reconciliation of {secret_name}: {e}")