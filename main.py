import kopf
import kubernetes.client
from kubernetes.client.rest import ApiException
import logging
import time
from datetime import datetime, timezone

# Configurazione logging strutturato
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('kss-operator')

# Annotation keys
SYNC_SOURCE_ANNOTATION = "kss-operator/source-namespace"
SYNC_TIMESTAMP_ANNOTATION = "kss-operator/synced-at"

def create_event(api, name, namespace, reason, message, event_type="Normal", involved_object=None):
    """
    Create a Kubernetes event for the specified object.
    """
    try:
        now = datetime.now(timezone.utc)
        
        event = kubernetes.client.CoreV1Event(
            metadata=kubernetes.client.V1ObjectMeta(
                name=f"{name}.{int(time.time() * 1000000)}",
                namespace=namespace
            ),
            involved_object=involved_object or kubernetes.client.V1ObjectReference(
                kind="Secret",
                name=name,
                namespace=namespace,
                api_version="v1"
            ),
            reason=reason,
            message=message,
            type=event_type,
            first_timestamp=now,
            last_timestamp=now,
            count=1,
            source=kubernetes.client.V1EventSource(component="kss-operator")
        )
        api.create_namespaced_event(namespace=namespace, body=event)
    except ApiException as e:
        logger.warning(f"Failed to create event: {e}")

def is_synced_secret(secret) -> bool:
    """Check if the secret is a synced copy created by kss-operator."""
    annotations = secret.metadata.annotations or {}
    return SYNC_SOURCE_ANNOTATION in annotations

def create_or_update_secret(api, secret_name, ns_name, source_namespace, secret_data):
    """
    Create or update a secret.
    Returns True if successful, False otherwise.
    """
    new_secret = kubernetes.client.V1Secret(
        metadata=kubernetes.client.V1ObjectMeta(
            name=secret_name,
            labels={
                "kss-operator/sync": "synced"
            },
            annotations={
                SYNC_SOURCE_ANNOTATION: source_namespace,
                SYNC_TIMESTAMP_ANNOTATION: str(int(time.time()))
            }
        ),
        data=secret_data.data,
        type=secret_data.type
    )
    
    try:
        # Try to read existing secret
        existing_secret = api.read_namespaced_secret(secret_name, ns_name)
        
        # Check if existing secret is a synced copy
        if not is_synced_secret(existing_secret):
            logger.warning(
                f"Secret '{secret_name}' in namespace '{ns_name}' exists but is not synced. "
                f"Skipping to avoid overwriting manual secret."
            )
            create_event(
                api, secret_name, ns_name,
                reason="SyncSkipped",
                message=f"Secret exists but was not created by kss-operator. Skipping sync from {source_namespace}.",
                event_type="Warning"
            )
            return False
        
        # Update existing synced secret
        logger.debug(f"Updating existing synced secret in '{ns_name}'")
        api.patch_namespaced_secret(
            name=secret_name,
            namespace=ns_name,
            body=new_secret
        )
        return True
        
    except ApiException as e:
        if e.status == 404:
            # Secret doesn't exist, try to create it
            try:
                api.create_namespaced_secret(namespace=ns_name, body=new_secret)
                return True
            except ApiException as create_error:
                if create_error.status == 409:
                    # Race condition: secret was created between read and create
                    # Will be fixed in next reconciliation loop
                    logger.debug(
                        f"Race condition detected for secret '{secret_name}' in '{ns_name}', "
                        f"will be reconciled in next cycle"
                    )
                    return False
                else:
                    logger.error(
                        f"Failed to create secret '{secret_name}' in namespace '{ns_name}': {create_error}"
                    )
                    return False
        else:
            logger.error(
                f"Error reading secret '{secret_name}' in namespace '{ns_name}': {e}"
            )
            return False

def sync_secret(secret_name, namespace, secret_data):
    api = kubernetes.client.CoreV1Api()
    
    try:
        namespaces = api.list_namespace().items
    except ApiException as e:
        logger.error(f"Failed to list namespaces: {e}", exc_info=True)
        return
    
    success_count = 0
    fail_count = 0
    target_namespaces = [ns.metadata.name for ns in namespaces if ns.metadata.name != namespace]
    
    logger.info(
        f"Starting sync of secret '{secret_name}' from namespace '{namespace}' "
        f"to {len(target_namespaces)} target namespaces"
    )
    
    for ns in namespaces:
        ns_name = ns.metadata.name
        if ns_name == namespace: 
            continue
        
        logger.debug(f"Processing namespace '{ns_name}' for secret '{secret_name}'")
        
        if create_or_update_secret(api, secret_name, ns_name, namespace, secret_data):
            success_count += 1
            logger.info(f"Successfully synced secret '{secret_name}' to namespace '{ns_name}'")
        else:
            fail_count += 1
            create_event(
                api, secret_name, ns_name,
                reason="SyncFailed",
                message=f"Failed to sync secret from namespace '{namespace}'",
                event_type="Warning"
            )
    
    logger.info(
        f"Sync completed for secret '{secret_name}': "
        f"{success_count} succeeded, {fail_count} failed out of {len(target_namespaces)} namespaces"
    )

def delete_synced_secrets(secret_name, source_namespace):
    api = kubernetes.client.CoreV1Api()
    
    try:
        namespaces = api.list_namespace().items
    except ApiException as e:
        logger.error(f"Failed to list namespaces: {e}", exc_info=True)
        return
    
    deleted_count = 0
    target_namespaces = [ns.metadata.name for ns in namespaces if ns.metadata.name != source_namespace]
    
    logger.info(
        f"Starting deletion of synced copies of secret '{secret_name}' "
        f"from {len(target_namespaces)} namespaces"
    )
    
    for ns in namespaces:
        ns_name = ns.metadata.name
        if ns_name == source_namespace:
            continue
            
        try:
            secret = api.read_namespaced_secret(secret_name, ns_name)
            
            # Delete only if it's a synced secret
            if is_synced_secret(secret):
                logger.info(f"Deleting synced secret '{secret_name}' from namespace '{ns_name}'")
                api.delete_namespaced_secret(secret_name, ns_name)
                deleted_count += 1
            else:
                logger.debug(f"Secret '{secret_name}' in namespace '{ns_name}' is not synced, skipping deletion")
                
        except ApiException as e:
            if e.status == 404:
                logger.debug(f"Secret '{secret_name}' not found in namespace '{ns_name}' (already deleted)")
                continue
            else:
                logger.error(
                    f"Error deleting secret '{secret_name}' from namespace '{ns_name}': {e}",
                    exc_info=True
                )
    
    logger.info(f"Deleted {deleted_count} synced copies of secret '{secret_name}'")

@kopf.on.create('v1', 'Secret', labels={"kss-operator/sync": "sync"})
@kopf.on.update('v1', 'Secret', labels={"kss-operator/sync": "sync"})
def sync_secret_handler(spec, meta, namespace, body, **kwargs):
    secret_name = meta['name']
    
    # Filter out synced copies
    if is_synced_secret(body):
        logger.debug(f"Skipping synced copy of secret '{secret_name}' in namespace '{namespace}'")
        return
    
    logger.info(f"Detected change in source secret '{secret_name}' in namespace '{namespace}'")
    
    api = kubernetes.client.CoreV1Api()
    
    try:
        secret = api.read_namespaced_secret(secret_name, namespace)
        sync_secret(secret_name, namespace, secret)
        
    except ApiException as e:
        logger.error(
            f"Failed to read secret '{secret_name}' in namespace '{namespace}': {e}",
            exc_info=True
        )
        create_event(
            api, secret_name, namespace,
            reason="SyncFailed",
            message=f"Failed to read source secret: {str(e)}",
            event_type="Warning"
        )

@kopf.on.delete('v1', 'Secret', labels={"kss-operator/sync": "sync"})
def delete_synced_secret_handler(meta, namespace, body, **kwargs):
    secret_name = meta['name']
    
    # Filter out synced copies
    if is_synced_secret(body):
        logger.debug(f"Skipping deletion of synced copy '{secret_name}' in namespace '{namespace}'")
        return
    
    logger.info(f"Source secret '{secret_name}' deleted from namespace '{namespace}'")
    
    api = kubernetes.client.CoreV1Api()
    delete_synced_secrets(secret_name, namespace)

@kopf.on.create('v1', 'Namespace')
def sync_secrets_in_new_namespace_handler(spec, meta, **kwargs):
    new_namespace = meta['name']
    logger.info(f"New namespace '{new_namespace}' created, initiating secret sync")
    
    api = kubernetes.client.CoreV1Api()
    
    try:
        secrets = api.list_secret_for_all_namespaces(label_selector="kss-operator/sync=sync").items
    except ApiException as e:
        logger.error(f"Failed to list secrets: {e}", exc_info=True)
        return
    
    # Filter out synced secrets
    source_secrets = [s for s in secrets if not is_synced_secret(s)]
    
    logger.info(f"Found {len(source_secrets)} source secrets to sync to namespace '{new_namespace}'")
    
    for secret in source_secrets:
        secret_name = secret.metadata.name
        source_namespace = secret.metadata.namespace
        
        logger.info(
            f"Syncing secret '{secret_name}' from namespace '{source_namespace}' "
            f"to new namespace '{new_namespace}'"
        )
        sync_secret(secret_name, source_namespace, secret)

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
    
    logger.debug(f"Starting reconciliation for secret '{secret_name}' in namespace '{namespace}'")
    
    api = kubernetes.client.CoreV1Api()
    
    try:
        source_secret = api.read_namespaced_secret(secret_name, namespace)
        namespaces = api.list_namespace().items
        
        changes_detected = False
        
        for ns in namespaces:
            ns_name = ns.metadata.name
            if ns_name == namespace:
                continue
            
            try:
                # Check if the secret exists in the target namespace
                existing = api.read_namespaced_secret(secret_name, ns_name)
                
                # Only update if it's a synced secret
                if is_synced_secret(existing):
                    # Compare data
                    if existing.data != source_secret.data or existing.type != source_secret.type:
                        logger.info(
                            f"Reconciliation: Secret '{secret_name}' in namespace '{ns_name}' "
                            f"is out of sync, updating"
                        )
                        changes_detected = True
                        
                        if create_or_update_secret(api, secret_name, ns_name, namespace, source_secret):
                            logger.info(f"Successfully reconciled secret '{secret_name}' in namespace '{ns_name}'")
                        else:
                            logger.debug(f"Failed to reconcile secret '{secret_name}' in namespace '{ns_name}'")
                        
            except ApiException as e:
                if e.status == 404:
                    # Secret does not exist, create it
                    logger.info(
                        f"Reconciliation: Secret '{secret_name}' missing in namespace '{ns_name}', "
                        f"creating"
                    )
                    changes_detected = True
                    
                    if create_or_update_secret(api, secret_name, ns_name, namespace, source_secret):
                        logger.info(f"Successfully created secret '{secret_name}' in namespace '{ns_name}'")
                    else:
                        logger.debug(f"Failed to create secret '{secret_name}' in namespace '{ns_name}'")
                else:
                    logger.error(
                        f"Reconciliation error for secret '{secret_name}' in namespace '{ns_name}': {e}"
                    )
        
        if changes_detected:
            logger.info(f"Reconciliation completed for secret '{secret_name}' with changes applied")
        else:
            logger.debug(f"Reconciliation completed for secret '{secret_name}' - no changes needed")
                    
    except ApiException as e:
        logger.error(
            f"Error during reconciliation of secret '{secret_name}': {e}",
            exc_info=True
        )