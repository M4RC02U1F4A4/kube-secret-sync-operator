# Kubernetes Secret Sync Operator

The **Kubernetes Secret Sync Operator** is a custom operator designed to synchronize Kubernetes Secrets across multiple namespaces. It monitors the creation of Secrets with a specific label and ensures that the same Secret is mirrored across all other namespaces in the cluster.

---

## Features

- **Syncs Secrets Across Namespaces:** Automatically synchronizes Secrets with a specific label to all other namespaces in the Kubernetes cluster.
- **Handles Secret Creation and Update:** If a Secret with the same name exists in the target namespace, it is deleted and replaced with the new Secret.
- **Handles Secret Deletion:** If a Secret with the defined label is deleted, it is also removed from all other namespaces in the cluster.

---

## Installation

### Latest version
```shell
kubectl apply -f https://raw.githubusercontent.com/M4RC02U1F4A4/kube-secret-sync-operator/refs/heads/main/manifest.yaml
```

## Usage

Simply add the label `"kss-operator/sync": "sync"` to the secret that you want to sync to all the namespaces.