# Migrating a VNet + Subnet Bicep Template to AWS VPC (CloudFormation)

This document explains how the Azure Bicep template that creates a virtual network with one
subnet (`resources/vpc/main.bicep`) is converted to an AWS CloudFormation YAML template that
creates a VPC with one subnet, and lists the commands to deploy, verify and delete it.

There is no automatic Bicep-to-CloudFormation converter, so the conversion is a manual mapping.

---

## 1. The key conceptual difference

| Azure Virtual Network | AWS VPC |
|---|---|
| A **VNet** has one or more address spaces (CIDR blocks); subnets are carved out of it | A **VPC** has exactly one primary CIDR block (additional CIDRs can be associated separately); subnets are carved out of it the same way |
| Subnets span the whole VNet region | Subnets are scoped to a single **Availability Zone**, so an AZ must be chosen per subnet |
| `serviceEndpoints` give a subnet private routing to Azure PaaS services (e.g. Storage) over the Microsoft backbone, without extra resources | No direct equivalent. The nearest analogue is a **VPC endpoint** (gateway endpoint for S3/DynamoDB, interface endpoint for most other services), which is its own resource, not a subnet property |
| No separate "internet gateway" concept for basic connectivity | A VPC needs an explicit `AWS::EC2::InternetGateway` + route table entry before anything in it can reach the internet (out of scope for the base template below — see section 8 for the NAT/Internet Gateway pattern when the source does have internet-facing resources) |
| A **Network Security Group (NSG)** is a free-standing resource attached to a subnet and/or NIC; `securityRules` are a flat ordered list on it | A **Security Group** is attached directly to ENIs/instances (not subnets) and is stateful by default (no explicit return-traffic rule needed); subnet-level traffic filtering instead uses a stateless **Network ACL** — see section 8 for which one to pick |
| A **Route Table** (`Microsoft.Network/routeTables`) is optional; Azure provides default system routes (VNet-local, internet) automatically with no ARM resource for them | A **Route Table** (`AWS::EC2::RouteTable`) is *always* required per subnet (or the VPC's implicit main route table applies) — there's no "no route table" state, only "using the default one" |
| A **NAT Gateway** (`Microsoft.Network/natGateways`) attaches to a subnet and needs its own `publicIPAddresses`/prefix resource | An **`AWS::EC2::NatGateway`** attaches to a *public* subnet and needs its own `AWS::EC2::EIP`, then outbound-only routes from the *private* subnet's route table point at it — same two-resource shape, different attachment point |

Out of scope for this mapping (not present in either sample template, and not
auto-migrated): VPC peering, ExpressRoute, Azure Firewall, DDoS Protection Plan. These
require their own knowledge-base entries before they can be included in a migration plan.

---

## 2. Resource mapping

| Bicep | CloudFormation |
|---|---|
| `Microsoft.Network/virtualNetworks` | `AWS::EC2::VPC` |
| `Microsoft.Network/virtualNetworks/subnets` | `AWS::EC2::Subnet` |
| `serviceEndpoints: [{ service: 'Microsoft.Storage' }]` | *(no 1:1 resource; see gotchas for the `AWS::EC2::VPCEndpoint` alternative)* |
| `Microsoft.Network/networkSecurityGroups` | `AWS::EC2::SecurityGroup` (default choice) or a set of `AWS::EC2::NetworkAclEntry` on an `AWS::EC2::NetworkAcl` (only when the rules are genuinely stateless/subnet-wide — see section 8) |
| `Microsoft.Network/networkSecurityGroups/securityRules` | `AWS::EC2::SecurityGroupIngress` / `AWS::EC2::SecurityGroupEgress` (one CFN resource per Azure rule) — the 6 platform-injected default rules (`AllowVnetInBound`, `AllowAzureLoadBalancerInBound`, `DenyAllInBound`, `AllowVnetOutBound`, `AllowInternetOutBound`, `DenyAllOutBound`, priority 65000-65500) are dropped automatically (`IMPLICIT_NOOP_TYPES`), only custom rules (priority 100-4096) are migrated |
| `Microsoft.Network/routeTables` | `AWS::EC2::RouteTable` + `AWS::EC2::SubnetRouteTableAssociation` per associated subnet |
| `Microsoft.Network/routeTables/routes` | `AWS::EC2::Route` |
| `Microsoft.Network/natGateways` | `AWS::EC2::NatGateway` |
| `Microsoft.Network/publicIPAddresses` | `AWS::EC2::EIP` (when used for a NAT Gateway) — Azure's dynamic/static allocation distinction has no CFN equivalent, EIPs are always "static" |
| *(implicit — Azure has no ARM resource for default internet routing)* | `AWS::EC2::InternetGateway` + `AWS::EC2::VPCGatewayAttachment` — new resources with no Bicep source counterpart, required whenever a NAT Gateway or any public-facing resource is in scope |

## 3. Parameter mapping

| Bicep | CloudFormation | Notes |
|---|---|---|
| `param vnetName string` | *(used to name/tag the VPC, not a hard identity — AWS VPCs are identified by ID)* | Carried over as a `Name` tag so the resource is recognizable in the console |
| `param subnetName string` | Same as above, as a tag on the subnet | |
| `param vnetAddressPrefix string` (CIDR) | `VpcCidrBlock` (String) | Passed straight through to `CidrBlock` |
| `param subnetAddressPrefix string` (CIDR) | `SubnetCidrBlock` (String) | Must be a sub-range of `VpcCidrBlock` |
| `param location string` | *(removed)* | The stack deploys to the region you pass with `--region`; an explicit `AvailabilityZone` parameter replaces it for the subnet |
| *(none)* | `AvailabilityZone` (String) | New parameter — AWS subnets must pick one AZ; Azure subnets have no AZ concept |

## 4. Property mapping

| Bicep property | CloudFormation equivalent |
|---|---|
| `properties.addressSpace.addressPrefixes` (list) | `CidrBlock` (single value — CloudFormation's `AWS::EC2::VPC` only accepts one primary CIDR; additional ones need a separate `AWS::EC2::VPCCidrBlock` resource) |
| `properties.addressPrefix` (subnet) | `CidrBlock` on `AWS::EC2::Subnet` |
| `properties.serviceEndpoints` | Not applicable directly; see gotchas |
| `location` | Not applicable (region comes from the stack, not the resource) |
| `output vnetName` / `subnetName` | `VpcId` / `SubnetId` outputs (`!Ref` on a VPC/subnet returns its ID) |
| NSG rule `properties.direction` (`Inbound`/`Outbound`) | Chooses `AWS::EC2::SecurityGroupIngress` vs. `...Egress` — there is no single resource with a direction flag like Azure's |
| NSG rule `properties.access` (`Allow`/`Deny`) | Security Groups are **allow-only** — there is no `Deny` rule type. A Deny rule with no corresponding narrower Allow has no direct translation; see gotchas |
| NSG rule `properties.protocol` (`Tcp`/`Udp`/`*`) | `IpProtocol` (`tcp`/`udp`/`-1` for any) — lowercase, and `*` becomes `-1`, not the string `"*"` |
| NSG rule `properties.sourcePortRange`/`destinationPortRange` | `FromPort`/`ToPort` — Azure's `*` (any port) becomes `FromPort: 0, ToPort: 65535` |
| NSG rule `properties.sourceAddressPrefix`/`destinationAddressPrefix` | `CidrIp` (or `SourceSecurityGroupId` when the prefix is actually another NSG/ASG reference, not a CIDR) |
| NSG rule `properties.sourceAddressPrefixes`/`destinationAddressPrefixes` (augmented rules list) | One SG rule per CIDR (or one NACL entry per CIDR for deny-style migrations) — AWS SG ingress/egress resources accept one CIDR per rule resource, so list-valued Azure rules must be expanded |
| NSG rule `properties.sourceApplicationSecurityGroups` / `destinationApplicationSecurityGroups` | `SourceSecurityGroupId` / peer-security-group references (after introducing explicit AWS security groups for each ASG-like boundary) |
| NSG rule `properties.priority` | Not applicable — Security Group rules are unordered (all evaluated, allow wins); if two Azure rules conflict by priority, the lower-priority one must be dropped or narrowed during migration, not just renumbered |
| Subnet `properties.networkSecurityGroup.id` | No subnet-attached SG in AWS; attach SGs at ENI/workload resources (EC2, Lambda ENI, ALB, etc.), or use a subnet `AWS::EC2::NetworkAcl` for subnet-wide stateless controls |
| Subnet `properties.routeTable.id` | `AWS::EC2::SubnetRouteTableAssociation` |
| Subnet `properties.natGateway.id` | Not a subnet property in AWS; represented as `AWS::EC2::NatGateway` + private-subnet `AWS::EC2::Route` (`NatGatewayId`) |
| Subnet `properties.privateEndpointNetworkPolicies` / `privateLinkServiceNetworkPolicies` | No direct subnet property equivalent; AWS PrivateLink endpoint/service policy decisions are expressed on endpoint/service resources, not subnet flags |
| Route table `properties.disableBgpRoutePropagation` | No direct `AWS::EC2::RouteTable` flag equivalent; route propagation is configured through VGW/TGW attachment route-propagation resources |
| Route `properties.addressPrefix` | `DestinationCidrBlock` on `AWS::EC2::Route` |
| Route `properties.nextHopType` (`VirtualNetworkGateway`/`VnetLocal`/`Internet`/`None`) | Selects which target property to set on `AWS::EC2::Route` (`GatewayId`, `NatGatewayId`, etc.) — Azure's `VnetLocal` has no CFN equivalent because local-VPC routing is implicit and automatic, so those routes are simply skipped |
| Route `properties.nextHopType = VirtualAppliance` + `nextHopIpAddress` | Usually requires a routed appliance architecture (GWLB/Transit Gateway/instance ENI). CFN route target is an ID (`TransitGatewayId`, `NetworkInterfaceId`, etc.), not an arbitrary IP string |
| Route `properties.nextHopType = None` | No 1:1 deny-route object in VPC route tables; model explicit deny with NACLs or workload SG policy, not a blackhole route literal |
| NAT Gateway `properties.publicIpAddress` | `AllocationId` (from the paired `AWS::EC2::EIP`'s `AllocationId` attribute, not the IP itself) |
| NAT Gateway `properties.idleTimeoutInMinutes` | No direct NAT Gateway property in CFN; timeout behavior is handled at workload/protocol level |
| NAT Gateway `zones` / per-zone design intent | One NAT Gateway per AZ for resilient private-subnet egress (each private subnet routes to the NAT in its own AZ) |
| Public IP `sku.name` / `publicIPAddressVersion` | `AWS::EC2::EIP` (IPv4 only in CFN). IPv6 egress patterns use egress-only internet gateways instead of EIP-backed NAT |

---

## 5. Converted template

Saved as `vpc.yaml`.


```yaml
AWSTemplateFormatVersion: '2010-09-09'
Description: >-
  Creates a VPC with one subnet.
  Converted from an Azure Bicep template (virtualNetworks + subnets).

Parameters:
  VpcCidrBlock:
    Type: String
    Default: 10.30.0.0/16
    Description: Primary CIDR block for the VPC (replaces addressSpace.addressPrefixes[0]).

  SubnetCidrBlock:
    Type: String
    Default: 10.30.1.0/24
    Description: CIDR block for the subnet. Must fall inside VpcCidrBlock.

  AvailabilityZone:
    Type: AWS::EC2::AvailabilityZone::Name
    Description: >-
      AZ to place the subnet in. Azure subnets have no AZ equivalent, so this
      is a new required choice when migrating.

Resources:
  Vpc:
    Type: AWS::EC2::VPC
    Properties:
      CidrBlock: !Ref VpcCidrBlock
      EnableDnsSupport: true
      EnableDnsHostnames: true
      Tags:
        - Key: Name
          Value: vnet-migration-demo

  Subnet:
    Type: AWS::EC2::Subnet
    Properties:
      VpcId: !Ref Vpc
      CidrBlock: !Ref SubnetCidrBlock
      AvailabilityZone: !Ref AvailabilityZone
      Tags:
        - Key: Name
          Value: subnet-app

Outputs:
  VpcId:
    Description: ID of the VPC
    Value: !Ref Vpc

  SubnetId:
    Description: ID of the subnet
    Value: !Ref Subnet
```

---

## 6. Commands

### Prerequisites

```bash
aws --version                 # AWS CLI v2 installed
aws configure                 # or: aws sso login --profile <profile>
aws sts get-caller-identity   # confirm the right account
```

The deploying identity needs `cloudformation:*` on the stack and
`ec2:CreateVpc`, `ec2:CreateSubnet`, `ec2:DeleteVpc`, `ec2:DeleteSubnet`,
`ec2:DescribeVpcs`, `ec2:DescribeSubnets`, `ec2:CreateTags`. No
`--capabilities` flag is required because the template creates no IAM
resources.

### Command equivalents

| Task | Azure | AWS |
|---|---|---|
| Deploy | `az deployment group create --resource-group <rg> --template-file main.bicep --parameters ...` | `aws cloudformation deploy --stack-name <name> --template-file vpc.yaml --parameter-overrides ...` |
| Scope | Resource group | Stack (region + account) |

### Optional: lint and validate

```bash
pip install cfn-lint
cfn-lint vpc.yaml

aws cloudformation validate-template \
  --template-body file://vpc.yaml
```

### Deploy

```bash
export AWS_REGION=us-east-1

aws cloudformation deploy \
  --stack-name vpc-migration-demo \
  --template-file vpc.yaml \
  --region "$AWS_REGION" \
  --parameter-overrides \
      VpcCidrBlock=10.30.0.0/16 \
      SubnetCidrBlock=10.30.1.0/24 \
      AvailabilityZone=us-east-1a
```

Equivalent using `create-stack`:

```bash
aws cloudformation create-stack \
  --stack-name vpc-migration-demo \
  --template-body file://vpc.yaml \
  --parameters \
      ParameterKey=VpcCidrBlock,ParameterValue=10.30.0.0/16 \
      ParameterKey=SubnetCidrBlock,ParameterValue=10.30.1.0/24 \
      ParameterKey=AvailabilityZone,ParameterValue=us-east-1a

aws cloudformation wait stack-create-complete --stack-name vpc-migration-demo
```

### Verify

```bash
# Stack status and outputs
aws cloudformation describe-stacks \
  --stack-name vpc-migration-demo \
  --query "Stacks[0].{Status:StackStatus,Outputs:Outputs}"

# VPC and subnet details (equivalent of `az network vnet show` / `az network vnet subnet show`)
aws ec2 describe-vpcs --vpc-ids <VpcId-from-outputs>
aws ec2 describe-subnets --subnet-ids <SubnetId-from-outputs>
```

### Delete

```bash
aws cloudformation delete-stack --stack-name vpc-migration-demo
aws cloudformation wait stack-delete-complete --stack-name vpc-migration-demo
```

Unlike Secrets Manager, VPCs/subnets have no soft-delete/recovery window —
deletion is immediate, and the stack can be redeployed right away with the
same name.

---

## 7. Gotchas and improvements

- **Only one primary CIDR per VPC in CloudFormation.** If the source VNet has
  multiple `addressPrefixes`, only the first can go on `AWS::EC2::VPC`
  directly; additional ones need a separate `AWS::EC2::VPCCidrBlock` resource
  per extra CIDR.
- **AZ choice is new, not optional.** Azure subnets are regional; AWS subnets
  are zonal. When migrating a VNet with multiple subnets for redundancy,
  spread them across different AZs rather than collapsing them into one.
- **`serviceEndpoints` has no subnet-level equivalent.** To keep traffic to
  AWS services off the public internet, add an `AWS::EC2::VPCEndpoint`
  (gateway type for S3/DynamoDB — free; interface type, PrivateLink-based,
  for most other services — hourly + data charges) associated with the
  subnet's route table, rather than a subnet property.
- **DNS:** Azure VNets resolve Azure-provided DNS by default. Set
  `EnableDnsSupport: true` and `EnableDnsHostnames: true` on the VPC (as
  above) to get the closest equivalent (Amazon-provided DNS + hostnames).
- **No NAT/Internet Gateway included.** This template is private-only,
  matching the source Bicep (no public IP or gateway resources declared).
  Add `AWS::EC2::InternetGateway` + `AWS::EC2::NatGateway` + route table
  entries only if the migrated workload actually needs outbound/inbound
  internet access — see section 8 for the full pattern.
- **Pricing:** VPCs and subnets themselves are free; charges come from
  attached resources (NAT Gateway, VPC endpoints, EC2 instances, etc.).
- **Security Groups are stateful, NACLs are not.** If the source NSG's rules
  only ever add narrower *Allow* entries on top of Azure's implicit default-deny,
  migrate to a Security Group (simpler, return traffic is automatic). Only
  reach for a Network ACL when the source genuinely relies on an explicit
  **Deny** rule for specific traffic (e.g. blocking one CIDR while allowing
  the rest) — NACLs are the only CFN construct with a real deny action, but
  they're stateless (you must add the mirrored outbound rule yourself) and
  evaluated in strict numeric rule-number order, closer to Azure's
  priority-ordered list than a Security Group is.
- **NSG default rules are dropped, not migrated.** The 6 platform-injected
  rules (priority 65000-65500) are structurally identical in every Azure NSG
  and have no user intent behind them; only custom rules (priority 100-4096)
  become `AWS::EC2::SecurityGroupIngress`/`Egress` resources.
- **A route table is implicit in Azure, explicit in AWS.** A VNet subnet with
  no `Microsoft.Network/routeTables` resource at all still has working
  VNet-local + internet system routes. An `AWS::EC2::Subnet` with no
  explicit `AWS::EC2::SubnetRouteTableAssociation` instead falls back to the
  VPC's main route table — always verify which routes that main table
  actually has before assuming "no association" means "no route table".
- **NAT Gateway needs a public subnet to live in, even though it serves a
  private one.** The Gateway itself is created in a subnet whose route table
  has a `0.0.0.0/0 -> AWS::EC2::InternetGateway` route (the "public" subnet);
  the *private* subnet's route table then points `0.0.0.0/0` at the NAT
  Gateway. Mixing these up (NAT Gateway placed in the private subnet) is the
  most common hand-authoring mistake when migrating this pattern.
- **Augmented NSG rules (list-valued prefixes/ports) must be exploded.** Azure
  lets one rule carry many source/destination CIDRs and ranges; CFN SG
  ingress/egress resources are one-CIDR/port-span each for traceable mapping.
- **`VirtualAppliance` next hops are architecture changes, not literal copies.**
  Azure routes can point at an appliance IP; AWS route targets are typed
  resource IDs (`TransitGatewayId`, `NetworkInterfaceId`, etc.), so this case
  requires selecting and provisioning the appliance/routing pattern first.
- **NAT is zonal in both clouds, but failure domains differ operationally.**
  Keep one NAT Gateway per AZ and route each private subnet to its same-AZ NAT;
  a single shared NAT for all AZs is cheaper but introduces cross-AZ data path
  costs and larger blast radius.

---

## 8. Advanced patterns: NSGs, route tables, NAT/Internet Gateways, multi-AZ

These patterns extend the base template above; they're documented separately
because they only apply when the source Bicep actually declares the
corresponding resource types (`Microsoft.Network/networkSecurityGroups`,
`Microsoft.Network/routeTables`, `Microsoft.Network/natGateways`).

### 8.1 NSG with custom rules -> Security Group

```yaml
  AppSecurityGroup:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: Migrated from Microsoft.Network/networkSecurityGroups nsg-app
      VpcId: !Ref Vpc

  AllowHttpsInbound:
    Type: AWS::EC2::SecurityGroupIngress
    Properties:
      GroupId: !Ref AppSecurityGroup
      IpProtocol: tcp
      FromPort: 443
      ToPort: 443
      CidrIp: 0.0.0.0/0
      Description: Migrated from NSG rule 'AllowHttpsInbound' (priority 100)
```

Each Azure `securityRules` entry (excluding the 6 default ones, see gotchas)
becomes its own `AWS::EC2::SecurityGroupIngress`/`...Egress` resource — do not
try to collapse multiple rules into one resource's `CidrIp`/port list even
when CFN's shorthand property syntax on `AWS::EC2::SecurityGroup` allows an
inline list, since that makes individual rules harder to trace back to their
Azure source for an audit.

### 8.2 Route table + NAT Gateway (public/private subnet pair)

```yaml
  PublicSubnet:
    Type: AWS::EC2::Subnet
    Properties:
      VpcId: !Ref Vpc
      CidrBlock: 10.30.0.0/24
      AvailabilityZone: !Ref AvailabilityZone
      MapPublicIpOnLaunch: true

  InternetGateway:
    Type: AWS::EC2::InternetGateway

  GatewayAttachment:
    Type: AWS::EC2::VPCGatewayAttachment
    Properties:
      VpcId: !Ref Vpc
      InternetGatewayId: !Ref InternetGateway

  PublicRouteTable:
    Type: AWS::EC2::RouteTable
    Properties:
      VpcId: !Ref Vpc

  PublicDefaultRoute:
    Type: AWS::EC2::Route
    DependsOn: GatewayAttachment
    Properties:
      RouteTableId: !Ref PublicRouteTable
      DestinationCidrBlock: 0.0.0.0/0
      GatewayId: !Ref InternetGateway

  PublicSubnetRouteAssociation:
    Type: AWS::EC2::SubnetRouteTableAssociation
    Properties:
      SubnetId: !Ref PublicSubnet
      RouteTableId: !Ref PublicRouteTable

  NatGatewayEip:
    Type: AWS::EC2::EIP
    Properties:
      Domain: vpc

  NatGateway:
    Type: AWS::EC2::NatGateway
    Properties:
      SubnetId: !Ref PublicSubnet
      AllocationId: !GetAtt NatGatewayEip.AllocationId

  PrivateRouteTable:
    Type: AWS::EC2::RouteTable
    Properties:
      VpcId: !Ref Vpc

  PrivateDefaultRoute:
    Type: AWS::EC2::Route
    Properties:
      RouteTableId: !Ref PrivateRouteTable
      DestinationCidrBlock: 0.0.0.0/0
      NatGatewayId: !Ref NatGateway

  PrivateSubnetRouteAssociation:
    Type: AWS::EC2::SubnetRouteTableAssociation
    Properties:
      SubnetId: !Ref Subnet
      RouteTableId: !Ref PrivateRouteTable
```

`DependsOn: GatewayAttachment` on the default route is required —
`AWS::EC2::Route` fails at deploy time if the Internet Gateway isn't attached
to the VPC yet, and CloudFormation won't infer that ordering from the
`GatewayId` reference alone.

### 8.3 Multiple subnets across Availability Zones

Azure VNet subnets are regional; when the source template defines several
subnets purely for redundancy (not for distinct address spaces/purposes),
spread their CFN equivalents across different AZs rather than one AZ each
picked arbitrarily:

```yaml
Parameters:
  AvailabilityZoneA:
    Type: AWS::EC2::AvailabilityZone::Name
  AvailabilityZoneB:
    Type: AWS::EC2::AvailabilityZone::Name

Resources:
  SubnetA:
    Type: AWS::EC2::Subnet
    Properties:
      VpcId: !Ref Vpc
      CidrBlock: 10.30.1.0/24
      AvailabilityZone: !Ref AvailabilityZoneA
  SubnetB:
    Type: AWS::EC2::Subnet
    Properties:
      VpcId: !Ref Vpc
      CidrBlock: 10.30.2.0/24
      AvailabilityZone: !Ref AvailabilityZoneB
```

Pick distinct AZs explicitly via parameters (as above) rather than
`Fn::GetAZs`/`Fn::Select` picking automatically — explicit AZ parameters keep
the mapping auditable and avoid two subnets silently landing in the same AZ
on redeploy if AWS's AZ ordering ever changes for the account/region.
