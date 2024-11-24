import kopf
import kubernetes.client
from kubernetes.client.rest import ApiException
import logging

def sync_secret(secret_name, namespace, secret_data):
    api = kubernetes.client.CoreV1Api()
    namespaces = api.list_namespace().items

    for ns in namespaces:
        ns_name = ns.metadata.name
        if ns_name == namespace: 
            continue
        logging.info(f"Synching {secret_name} secret in {ns_name} namespace...")
        try:
            api.read_namespaced_secret(secret_name, ns_name)
            api.delete_namespaced_secret(secret_name, ns_name)
        except ApiException as e:
            if e.status != 404:
                kopf.exception(e)
        new_secret = kubernetes.client.V1Secret(
            metadata=kubernetes.client.V1ObjectMeta(
                name=secret_name,
                labels={
                    "kss-operator/sync": "synched"
                }
            ),
            data=secret_data.data,
            type=secret_data.type
        )
        api.create_namespaced_secret(namespace=ns_name, body=new_secret)

def delete_synced_secrets(secret_name):
    api = kubernetes.client.CoreV1Api()
    namespaces = api.list_namespace().items

    for ns in namespaces:
        ns_name = ns.metadata.name
        try:
            api.read_namespaced_secret(secret_name, ns_name)
            logging.info(f"Deleting {secret_name} secret from {ns_name} namespace...")
            api.delete_namespaced_secret(secret_name, ns_name)
        except ApiException as e:
            if e.status != 404:
                kopf.exception(e)

@kopf.on.create('v1', 'Secret', labels={"kss-operator/sync": "sync"})
@kopf.on.update('v1', 'Secret', labels={"kss-operator/sync": "sync"})
def sync_secret_handler(spec, meta, namespace, **kwargs):
    secret_name = meta['name']
    api = kubernetes.client.CoreV1Api()
    secret = api.read_namespaced_secret(secret_name, namespace)
    logging.info(f"Secret {secret_name} created in the {namespace} namespace, syncing with all other namespaces.")
    sync_secret(secret_name, namespace, secret)

@kopf.on.delete('v1', 'Secret', labels={"kss-operator/sync": "sync"})
def delete_synced_secret_handler(meta, namespace, **kwargs):
    secret_name = meta['name']
    logging.info(f"Secret {secret_name} deleted from the {namespace} namespace, removing from all other namespaces.")
    delete_synced_secrets(secret_name)

@kopf.on.create('v1', 'Namespace')
def sync_secrets_in_new_namespace_handler(spec, meta, **kwargs):
    new_namespace = meta['name']
    logging.info(f"New namespace {new_namespace} created, syncing secrets...")
    
    api = kubernetes.client.CoreV1Api()
    secrets = api.list_secret_for_all_namespaces(label_selector="kss-operator/sync=sync").items 

    for secret in secrets:
        secret_name = secret.metadata.name
        logging.info(f"Syncing secret {secret_name} to {new_namespace} namespace...")
        sync_secret(secret_name, secret.metadata.namespace, secret)
